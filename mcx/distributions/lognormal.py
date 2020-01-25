from jax import numpy as np
from jax import random

from . import constraints
from .distribution import Distribution
from .utils import broadcast_batch_shape, limit_to_support


class LogNormal(Distribution):
    params_constraints = {"mu": constraints.real, "sigma": constraints.positive}
    support = constraints.positive

    def __init__(self, mu, sigma):
        self.event_shape = ()
        self.batch_shape = broadcast_batch_shape(mu, sigma)
        self.mu = mu
        self.sigma = sigma

    def sample(self, rng_key, sample_shape):
        shape = sample_shape + self.batch_shape + self.event_shape
        return np.exp(self.sigma * random.normal(rng_key, shape) + self.mu)

    @limit_to_support
    def logpdf(self, x):
        return -((np.log(x) - self.mu) ** 2 / (2 * self.sigma ** 2)) - np.log(
            self.sigma * x * np.sqrt(2 * np.pi)
        )