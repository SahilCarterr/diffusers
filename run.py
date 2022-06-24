#!/usr/bin/env python3
import numpy as np
import PIL
import functools

import models
from models import utils as mutils
from models import ncsnv2
from models import ncsnpp
from models import ddpm as ddpm_model
from models import layerspp
from models import layers
from models import normalization

from utils import restore_checkpoint

import sampling
from sde_lib import VESDE, VPSDE, subVPSDE
from sampling import (NoneCorrector, 
                      ReverseDiffusionPredictor, 
                      LangevinCorrector,
                      EulerMaruyamaPredictor, 
                      AncestralSamplingPredictor, 
                      NonePredictor,
                      AnnealedLangevinDynamics)
import datasets
import torch


torch.manual_seed(0)


#class NewVESDE(SDE):
#  def __init__(self, sigma_min=0.01, sigma_max=50, N=1000):
#    """Construct a Variance Exploding SDE.
#
#    Args:
#      sigma_min: smallest sigma.
#      sigma_max: largest sigma.
#      N: number of discretization steps
#    """
#    super().__init__(N)
#    self.sigma_min = sigma_min
#    self.sigma_max = sigma_max
#    self.discrete_sigmas = torch.exp(torch.linspace(np.log(self.sigma_min), np.log(self.sigma_max), N))
#    self.N = N
#
#  @property
#  def T(self):
#    return 1
#
#  def sde(self, x, t):
#    sigma = self.sigma_min * (self.sigma_max / self.sigma_min) ** t
#    drift = torch.zeros_like(x)
#    diffusion = sigma * torch.sqrt(torch.tensor(2 * (np.log(self.sigma_max) - np.log(self.sigma_min)),
#                                                device=t.device))
#    return drift, diffusion
#
#  def marginal_prob(self, x, t):
#    std = self.sigma_min * (self.sigma_max / self.sigma_min) ** t
#    mean = x
#    return mean, std
#
#  def prior_sampling(self, shape):
#    return torch.randn(*shape) * self.sigma_max
#
#  def prior_logp(self, z):
#    shape = z.shape
#    N = np.prod(shape[1:])
#    return -N / 2. * np.log(2 * np.pi * self.sigma_max ** 2) - torch.sum(z ** 2, dim=(1, 2, 3)) / (2 * self.sigma_max ** 2)
#
#  def discretize(self, x, t):
#    """SMLD(NCSN) discretization."""
#    timestep = (t * (self.N - 1) / self.T).long()
#    sigma = self.discrete_sigmas.to(t.device)[timestep]
#    adjacent_sigma = torch.where(timestep == 0, torch.zeros_like(t),
#                                 self.discrete_sigmas[timestep - 1].to(t.device))
#    f = torch.zeros_like(x)
#    G = torch.sqrt(sigma ** 2 - adjacent_sigma ** 2)
#    return f, G


class NewReverseDiffusionPredictor:

  def __init__(self, sde, score_fn, probability_flow=False):
    super().__init__()
    self.sde = sde
    self.probability_flow = probability_flow
    self.score_fn = score_fn

  def discretize(self, x, t):
    timestep = (t * (self.sde.N - 1) / self.sde.T).long()
    sigma = self.sde.discrete_sigmas.to(t.device)[timestep]
    adjacent_sigma = torch.where(timestep == 0, torch.zeros_like(t),
                                 self.sde.discrete_sigmas[timestep - 1].to(t.device))
    f = torch.zeros_like(x)
    G = torch.sqrt(sigma ** 2 - adjacent_sigma ** 2)

    labels = self.sde.marginal_prob(torch.zeros_like(x), t)[1]
    result = self.score_fn(x, labels)

    rev_f = f - G[:, None, None, None] ** 2 * result * (0.5 if self.probability_flow else 1.)
    rev_G = torch.zeros_like(G) if self.probability_flow else G
    return rev_f, rev_G

  def update_fn(self, x, t):
    f, G = self.discretize(x, t)
    z = torch.randn_like(x)
    x_mean = x - f
    x = x_mean + G[:, None, None, None] * z
    return x, x_mean


class NewLangevinCorrector:

  def __init__(self, sde, score_fn, snr, n_steps):
    super().__init__()
    self.sde = sde
    self.score_fn = score_fn
    self.snr = snr
    self.n_steps = n_steps

  def update_fn(self, x, t):
    sde = self.sde
    score_fn = self.score_fn
    n_steps = self.n_steps
    target_snr = self.snr
    if isinstance(sde, VPSDE) or isinstance(sde, subVPSDE):
      timestep = (t * (sde.N - 1) / sde.T).long()
      alpha = sde.alphas.to(t.device)[timestep]
    else:
      alpha = torch.ones_like(t)

    for i in range(n_steps):
      labels = sde.marginal_prob(torch.zeros_like(x), t)[1]
      grad = score_fn(x, labels)
      noise = torch.randn_like(x)
      grad_norm = torch.norm(grad.reshape(grad.shape[0], -1), dim=-1).mean()
      noise_norm = torch.norm(noise.reshape(noise.shape[0], -1), dim=-1).mean()
      step_size = (target_snr * noise_norm / grad_norm) ** 2 * 2 * alpha
      x_mean = x + step_size[:, None, None, None] * grad
      x = x_mean + torch.sqrt(step_size * 2)[:, None, None, None] * noise

    return x, x_mean



def save_image(x):
#    image_processed = x.cpu().permute(0, 2, 3, 1)
#    image_processed = (image_processed + 1.0) * 127.5
#    image_processed = image_processed.numpy().astype(np.uint8)
    image_processed = np.clip(x.permute(0, 2, 3, 1).cpu().numpy() * 255, 0, 255).astype(np.uint8)
    image_pil = PIL.Image.fromarray(image_processed[0])

    # 6. save image
    image_pil.save("../images/hey.png")


#x = np.load("cifar10.npy")
#
#save_image(x)
# @title Load the score-based model
sde = 'VESDE' #@param ['VESDE', 'VPSDE', 'subVPSDE'] {"type": "string"}
if sde.lower() == 'vesde':
  from configs.ve import cifar10_ncsnpp_continuous as configs
  ckpt_filename = "exp/ve/cifar10_ncsnpp_continuous/checkpoint_24.pth"
#  from configs.ve import ffhq_ncsnpp_continuous as configs
#  ckpt_filename = "exp/ve/ffhq_1024_ncsnpp_continuous/checkpoint_60.pth"
  config = configs.get_config()  
  config.model.num_scales = 1000
  sde = VESDE(sigma_min=config.model.sigma_min, sigma_max=config.model.sigma_max, N=config.model.num_scales)
  sampling_eps = 1e-5
elif sde.lower() == 'vpsde':
  from configs.vp import cifar10_ddpmpp_continuous as configs  
  ckpt_filename = "exp/vp/cifar10_ddpmpp_continuous/checkpoint_8.pth"
  config = configs.get_config()
  sde = VPSDE(beta_min=config.model.beta_min, beta_max=config.model.beta_max, N=config.model.num_scales)
  sampling_eps = 1e-3
elif sde.lower() == 'subvpsde':
  from configs.subvp import cifar10_ddpmpp_continuous as configs
  ckpt_filename = "exp/subvp/cifar10_ddpmpp_continuous/checkpoint_26.pth"
  config = configs.get_config()
  sde = subVPSDE(beta_min=config.model.beta_min, beta_max=config.model.beta_max, N=config.model.num_scales)
  sampling_eps = 1e-3

batch_size = 1 #@param {"type":"integer"}
config.training.batch_size = batch_size
config.eval.batch_size = batch_size

random_seed = 0 #@param {"type": "integer"}

score_model = mutils.create_model(config)

loaded_state = torch.load(ckpt_filename)
score_model.load_state_dict(loaded_state["model"], strict=False)

inverse_scaler = datasets.get_data_inverse_scaler(config)
predictor = ReverseDiffusionPredictor #@param ["EulerMaruyamaPredictor", "AncestralSamplingPredictor", "ReverseDiffusionPredictor", "None"] {"type": "raw"}
corrector = LangevinCorrector #@param ["LangevinCorrector", "AnnealedLangevinDynamics", "None"] {"type": "raw"}

def image_grid(x):
  size = config.data.image_size
  channels = config.data.num_channels
  img = x.reshape(-1, size, size, channels)
  w = int(np.sqrt(img.shape[0]))
  img = img.reshape((w, w, size, size, channels)).transpose((0, 2, 1, 3, 4)).reshape((w * size, w * size, channels))
  return img

#@title PC sampling
img_size = config.data.image_size
channels = config.data.num_channels
shape = (batch_size, channels, img_size, img_size)
probability_flow = False
snr = 0.16 #@param {"type": "number"}
n_steps =  1#@param {"type": "integer"}


def shared_predictor_update_fn(x, t, sde, model, predictor, probability_flow, continuous):
  """A wrapper that configures and returns the update function of predictors."""
  score_fn = mutils.get_score_fn(sde, model, train=False, continuous=continuous)
  if predictor is None:
    # Corrector-only sampler
    predictor_obj = NonePredictor(sde, score_fn, probability_flow)
  else:
    predictor_obj = predictor(sde, score_fn, probability_flow)
  return predictor_obj.update_fn(x, t)


def shared_corrector_update_fn(x, t, sde, model, corrector, continuous, snr, n_steps):
  """A wrapper tha configures and returns the update function of correctors."""
  score_fn = mutils.get_score_fn(sde, model, train=False, continuous=continuous)
  if corrector is None:
    # Predictor-only sampler
    corrector_obj = NoneCorrector(sde, score_fn, snr, n_steps)
  else:
    corrector_obj = corrector(sde, score_fn, snr, n_steps)
  return corrector_obj.update_fn(x, t)


continuous = config.training.continuous


predictor_update_fn = functools.partial(shared_predictor_update_fn,
                                          sde=sde,
                                          predictor=predictor,
                                          probability_flow=probability_flow,
                                          continuous=continuous)

corrector_update_fn = functools.partial(shared_corrector_update_fn,
                                          sde=sde,
                                          corrector=corrector,
                                          continuous=continuous,
                                          snr=snr,
                                          n_steps=n_steps)

device = "cuda"
model = score_model.to(device)
denoise = False

new_corrector = NewLangevinCorrector(sde=sde, score_fn=model, snr=snr, n_steps=n_steps)
new_predictor = NewReverseDiffusionPredictor(sde=sde, score_fn=model)


with torch.no_grad():
    # Initial sample
    x = sde.prior_sampling(shape).to(device)
    timesteps = torch.linspace(sde.T, sampling_eps, sde.N, device=device)

    for i in range(sde.N):
        t = timesteps[i]
        vec_t = torch.ones(shape[0], device=t.device) * t
        x, x_mean = corrector_update_fn(x, vec_t, model=model)
        x, x_mean = predictor_update_fn(x, vec_t, model=model)
#        x, x_mean = new_corrector.update_fn(x, vec_t)
#        x, x_mean = new_predictor.update_fn(x, vec_t)

    x, n = inverse_scaler(x_mean if denoise else x), sde.N * (n_steps + 1)


save_image(x)

# for 5
#assert (x.abs().sum() - 106114.90625).cpu().item() < 1e-2, f"sum wrong {x.abs().sum()}"
#assert (x.abs().mean() - 34.5426139831543).abs().cpu().item() < 1e-4, f"mean wrong {x.abs().mean()}"

# for 1000
assert (x.abs().sum() - 436.5811).abs().sum().cpu().item() < 1e-2, f"sum wrong {x.abs().sum()}"
assert (x.abs().mean() - 0.1421).abs().mean().cpu().item() < 1e-4, f"mean wrong {x.abs().mean()}"

