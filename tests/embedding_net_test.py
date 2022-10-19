from __future__ import annotations

import pytest
import torch

from torch import eye, ones, zeros

from sbi import utils as utils
from sbi.inference import SNLE, SNPE, SNRE
from sbi.neural_nets.embedding_nets import FCEmbedding, PermutationInvariantEmbedding
from sbi.simulators.linear_gaussian import (
    linear_gaussian,
    true_posterior_linear_gaussian_mvn_prior,
)
from sbi.utils import classifier_nn, likelihood_nn, posterior_nn
from tests.test_utils import check_c2st


@pytest.mark.parametrize("method", ["SNPE", "SNLE", "SNRE"])
@pytest.mark.parametrize("num_dim", [1, 2])
@pytest.mark.parametrize("embedding_net", ["mlp"])
def test_embedding_net_api(method, num_dim: int, embedding_net: str):
    """Tests the API when using a preconfigured embedding net."""

    x_o = zeros(1, num_dim)

    # likelihood_mean will be likelihood_shift+theta
    likelihood_shift = -1.0 * ones(num_dim)
    likelihood_cov = 0.3 * eye(num_dim)

    prior = utils.BoxUniform(-2.0 * ones(num_dim), 2.0 * ones(num_dim))

    theta = prior.sample((1000,))
    x = linear_gaussian(theta, likelihood_shift, likelihood_cov)

    if embedding_net == "mlp":
        embedding = FCEmbedding(input_dim=num_dim)
    else:
        raise NameError(f"{embedding_net} not supported.")

    if method == "SNPE":
        density_estimator = posterior_nn("maf", embedding_net=embedding)
        inference = SNPE(
            prior, density_estimator=density_estimator, show_progress_bars=False
        )
    elif method == "SNLE":
        density_estimator = likelihood_nn("maf", embedding_net=embedding)
        inference = SNLE(
            prior, density_estimator=density_estimator, show_progress_bars=False
        )
    elif method == "SNRE":
        classifier = classifier_nn("resnet", embedding_net_x=embedding)
        inference = SNRE(prior, classifier=classifier, show_progress_bars=False)
    else:
        raise NameError

    _ = inference.append_simulations(theta, x).train(max_num_epochs=5)
    posterior = inference.build_posterior().set_default_x(x_o)

    s = posterior.sample((1,))
    _ = posterior.potential(s)


@pytest.mark.parametrize("num_trials", [1, 2])
@pytest.mark.parametrize("num_dim", [1, 2])
def test_iid_embedding_api(num_trials, num_dim):

    prior = utils.BoxUniform(-2.0 * ones(num_dim), 2.0 * ones(num_dim))

    num_thetas = 1000
    theta = prior.sample((num_thetas,))

    # simulate iid x.
    iid_theta = theta.reshape(num_thetas, 1, num_dim).repeat(1, num_trials, 1)
    x = torch.randn_like(iid_theta) + iid_theta
    x_o = zeros(1, num_trials, num_dim)

    output_dim = 5
    single_trial_net = FCEmbedding(input_dim=num_dim, output_dim=output_dim)
    embedding_net = PermutationInvariantEmbedding(
        single_trial_net,
        trial_net_output_dim=output_dim,
    )

    density_estimator = posterior_nn("maf", embedding_net=embedding_net)
    inference = SNPE(prior, density_estimator=density_estimator)

    _ = inference.append_simulations(theta, x).train(max_num_epochs=5)
    posterior = inference.build_posterior().set_default_x(x_o)

    s = posterior.sample((1,))
    _ = posterior.potential(s)


@pytest.mark.slow
@pytest.mark.parametrize("num_trials", [10, 50])
@pytest.mark.parametrize("num_dim", [2])
@pytest.mark.parametrize("method", ["SNPE", "SNLE", "SNRE"])
def test_iid_inference(num_trials, num_dim, method):
    """Test accuracy in Gaussian linear simulator with iid trials.

    Tests permutation invariance of NPE iid embeddings, and MCMC iid-trials inference
    with NLE and NRE trained on single trials.
    """

    prior = torch.distributions.MultivariateNormal(
        torch.zeros(num_dim), torch.eye(num_dim)
    )

    # Scale number of training samples with num_trials.
    num_thetas = 1000 + 100 * num_trials
    # Likelihood-based methods train on single trials.
    num_simulations = num_thetas

    if method == "SNPE":  # SNPE needs embedding and iid trials during training.
        theta = prior.sample((num_thetas,))
        # simulate iid x.
        iid_theta = theta.reshape(num_thetas, 1, num_dim).repeat(1, num_trials, 1)
        x = torch.randn_like(iid_theta) + iid_theta
        x_o = zeros(1, num_trials, num_dim)

        # embedding
        latent_dim = 10
        single_trial_net = FCEmbedding(
            input_dim=num_dim,
            num_hiddens=40,
            num_layers=2,
            output_dim=latent_dim,
        )
        embedding_net = PermutationInvariantEmbedding(
            single_trial_net,
            trial_net_output_dim=latent_dim,
            # NOTE: post-embedding is not needed really.
            num_layers=1,
            num_hiddens=10,
            output_dim=10,
        )

        density_estimator = posterior_nn("maf", embedding_net=embedding_net)

        inference = SNPE(prior, density_estimator=density_estimator)
    else:  # likelihood-based methods: single-trial training without embeddings.

        theta = prior.sample((num_simulations,))
        x = torch.randn_like(theta) + theta
        x_o = zeros(1, num_dim)

        if method == "SNLE":
            inference = SNLE(prior, density_estimator=likelihood_nn("maf"))
        elif method == "SNRE":
            inference = SNRE(prior, classifier=classifier_nn("resnet"))
        else:
            raise NameError

    # get reference samples from true posterior
    num_samples = 1000
    reference_samples = true_posterior_linear_gaussian_mvn_prior(
        x_o.squeeze(),
        likelihood_shift=torch.zeros(num_dim),
        likelihood_cov=torch.eye(num_dim),
        prior_cov=prior.covariance_matrix,
        prior_mean=prior.loc,
    ).sample((num_samples,))

    # training
    _ = inference.append_simulations(theta, x).train()

    # inference
    posterior = inference.build_posterior().set_default_x(x_o)
    samples = posterior.sample((num_samples,))

    if method == "SNPE":
        check_c2st(samples, reference_samples, alg=method)
        # permute and test again
        num_repeats = 2
        for _ in range(num_repeats):
            trial_permutet_x_o = x_o[:, torch.randperm(x_o.shape[1]), :]
            samples = posterior.sample((num_samples,), x=trial_permutet_x_o)
            check_c2st(samples, reference_samples, alg=method + " permuted")
    else:
        check_c2st(samples, reference_samples, alg=method)
