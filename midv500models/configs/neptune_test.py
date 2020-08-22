import neptune

# The init() function called this way assumes that
# NEPTUNE_API_TOKEN environment variable is defined.

neptune.init('zackpashkin/sandbox')

PARAMS = {'decay_factor' : 0.5,
          'n_iterations' : 117}

neptune.create_experiment(name='minimal_example',params=PARAMS)

# log some metrics

for i in range(100):
    neptune.log_metric('loss', 0.95**i)

neptune.log_metric('AUC', 0.96)