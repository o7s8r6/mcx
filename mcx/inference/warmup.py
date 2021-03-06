"""Warming up the chain.

.. note:
    This is a "flat zone": all positions are 1D array.
"""
from functools import partial
from typing import Callable, Tuple

import jax
from jax import numpy as np

from mcx.inference.adaptive import (
    DualAveragingState,
    MassMatrixAdaptationState,
    dual_averaging,
    find_reasonable_step_size,
    mass_matrix_adaptation,
    longest_batch_before_turn,
)
from mcx.inference.dynamics import gaussian_euclidean_metric
from mcx.inference.kernels import HMCState, hmc_kernel
from mcx.inference.integrators import hmc_integrator


def stan_hmc_warmup(
    rng_key: jax.random.PRNGKey,
    logpdf: Callable,
    initial_state: HMCState,
    metric_factory: Callable,
    integrator_step: Callable,
    inital_step_size: float,
    path_length: float,
    num_steps: int,
    diagonal_mass_matrix=True,
) -> Tuple[HMCState, DualAveragingState, MassMatrixAdaptationState]:
    """ Warmup scheme for sampling procedures based on euclidean manifold HMC.

    Decompose the warmup and write the steps down here.

    Separation between sampling and warmup ensures better modularity; a modification
    in the warmup procedure should not affect the sampling implementation.

    Returns
    -------
    Tuple
        The current state of the chain and the warmed-up kernel.
    """

    n_dims = np.shape(initial_state.position)[-1]  # `position` is a 1D array

    # Initialize the mass matrix adaptation
    mm_init, mm_update, mm_final = mass_matrix_adaptation(diagonal_mass_matrix)
    mm_state = mm_init(n_dims)

    # Initialize the HMC transition kernel
    momentum_generator, kinetic_energy = metric_factory(
        mm_state.mass_matrix_sqrt, mm_state.inverse_mass_matrix
    )

    # Find a first reasonable step size and initialize dual averaging
    step_size = find_reasonable_step_size(
        rng_key,
        momentum_generator,
        kinetic_energy,
        integrator_step,
        initial_state,
        inital_step_size,
    )
    da_init, da_update = dual_averaging()
    da_state = da_init(step_size)

    # initial kernel
    integrator = hmc_integrator(integrator_step, path_length, step_size)
    kernel = hmc_kernel(momentum_generator, kinetic_energy, integrator)

    # Get warmup schedule
    schedule = warmup_schedule(num_steps)

    state = initial_state
    for i, window in enumerate(schedule):
        is_middle_window = (0 < i) & (i < (len(schedule) - 1))

        for step in range(window):
            _, rng_key = jax.random.split(rng_key)
            state, info = kernel(rng_key, state)

            da_state = da_update(info.acceptance_probability, da_state)
            step_size = np.exp(da_state.log_step_size)
            integrator = hmc_integrator(integrator_step, path_length, step_size)

            if is_middle_window:
                mm_state = mm_update(mm_state, state.position)

            kernel = hmc_kernel(momentum_generator, kinetic_energy, integrator)

        if is_middle_window:
            inverse_mass_matrix, mass_matrix_sqrt = mm_final(mm_state)
            momentum_generator, kinetic_energy = gaussian_euclidean_metric(
                mass_matrix_sqrt, inverse_mass_matrix
            )
            mm_state = mm_init(n_dims)
            step_size = find_reasonable_step_size(
                rng_key,
                momentum_generator,
                kinetic_energy,
                integrator_step,
                initial_state,
                inital_step_size,
            )
            da_state = da_init(step_size)
            integrator = hmc_integrator(integrator_step, path_length, step_size)
            kernel = hmc_kernel(momentum_generator, kinetic_energy, integrator)

    return state, da_state, mm_state


@partial(jax.jit, static_argnums=(0, 1, 2, 3, 4, 5, 6, 7))
def ehmc_warmup(
    rng_key: jax.random.PRNGKey,
    logpdf: Callable,
    initial_state: HMCState,
    momentum_generator: Callable,
    kinetic_energy: Callable,
    integrator_step: Callable,
    step_size: float,
    path_length: float,
    num_longest_batch: int,
):
    """Warmup scheme for empirical Hamiltonian Monte Carlo.

    We build a list of longest batches (path lengths) from which we will draw
    path lengths for the integration step during inference. This warmup is
    typically run after the hmc warmup.

    For that purpose, we build a custom integrator that implements algorithm
    2. in [1]_.

    References
    ----------
    .. [1]: Wu, Changye, Julien Stoehr, and Christian P. Robert. "Faster
            Hamiltonian Monte Carlo by learning leapfrog scale." arXiv preprint
            arXiv:1810.04449 (2018).
    """

    # Build the warmup kernel

    step = integrator_step(logpdf)
    longest_batch_step = longest_batch_before_turn(step)

    def longest_batch_integrator(rng_key: jax.random.PRNGKey, integrator_state):
        """The integrator state that is iterated over is the standard
        IntegratorState plus the longest batch length. Here we bump in a
        limitation of python < 3.7: it is not possible to subclass the
        IntegratorState named tuple to add a field. We need to find a solution
        as we may often need to subclass the base named tuple.
        """
        position, momentum, log_prob, log_prob_grad, batch_length = longest_batch_step(
            integrator_state.position,
            integrator_state.momentum,
            integrator_state.step_size,
            integrator_state.path_length,
        )
        if batch_length < path_length:
            position, momentum, log_prob, log_prob_grad = step(
                position,
                momentum,
                log_prob,
                log_prob_grad,
                step_size,
                path_length - batch_length,
            )
        return momentum, position, log_prob, log_prob_grad, batch_length

    ehmc_warmup_kernel = hmc_kernel(
        longest_batch_integrator, momentum_generator, kinetic_energy
    )

    # Run the kernel and return an array of longest batch lengths

    def warmup_update(state, key):
        hmc_state, _ = state
        new_hmc_state, new_hmc_info = ehmc_warmup_kernel(key, hmc_state)
        _, _, _, _, batch_length = new_hmc_info.integrator_step
        return (new_hmc_state, new_hmc_info), batch_length

    keys = jax.random.split(rng_key, num_longest_batch)
    state, batch_lengths = jax.lax.scan(warmup_update, (initial_state, None), keys,)
    hmc_warmup_state, _ = state

    return hmc_warmup_state, batch_lengths


def warmup_schedule(num_steps, initial_buffer=75, first_window=25, final_buffer=50):
    """Returns an adaptation warmup schedule.

    The schedule below is intended to be as close as possible to Stan's _[1].
    The warmup period is split into three stages:

    1. An initial fast interval to reach the typical set.
    2. "Slow" parameters that require global information (typically covariance)
       are estimated in a series of expanding windows with no memory.
    3. Fast parameters are learned after the adaptation of the slow ones.

    See _[1] for a more detailed explanation.

    Parameters
    ----------
    num_warmup: int
        The number of warmup steps to perform.
    initial_buffer: int
        The width of the initial fast adaptation interval.
    first_window: int
        The width of the first slow adaptation interval. There are 5 such
        intervals; the width of a window interval is twice the size of the
        preceding.
    final_buffer: int
        The width of the final fast adaptation interval.

    References
    ----------
    .. [1]: Stan Reference Manual v2.22
            Section 15.2 "HMC Algorithm"
    """
    schedule = []

    # Handle the situations where the numbrer of warmup steps is smaller than
    # the sum of the buffers' widths
    if num_steps < 20:
        schedule.append((0, num_steps - 1))
        return schedule

    if initial_buffer + first_window + final_buffer > num_steps:
        initial_buffer = int(0.15 * num_steps)
        final_buffer = int(0.1 * num_steps)
        first_window = num_steps - initial_buffer - final_buffer

    # First stage: adaptation of fast parameters
    schedule.append((0, initial_buffer - 1))

    # Second stage: adaptation of slow parameters
    final_buffer_start = num_steps - final_buffer

    next_size = first_window
    next_start = initial_buffer
    while next_start < final_buffer_start:
        start, size = next_size, next_start
        if 3 * size <= final_buffer_start - start:
            next_size = 2 * size
        else:
            size = final_buffer_start - start
        next_start = start + size
        schedule.append((start, next_start - 1))

    # Last stage: adaptation of fast parameters
    schedule.append((final_buffer_start, num_steps - 1))

    return schedule
