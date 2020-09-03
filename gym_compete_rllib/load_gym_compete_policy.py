import gym
import numpy as np
from matplotlib import pyplot as plt
import tensorflow.keras as keras
import tensorflow as tf
import pickle
from gym_compete_rllib.layers import ObservationPreprocessingLayer, UnconnectedVariableLayer
from gym_compete_rllib.layers import DiagonalNormalSamplingLayer, ValuePostprocessingLayer

# path with .pkl agent files
pickle_path = '/scratch/sergei/better-adversarial-defenses/multiagent-competition/gym_compete/agent_zoo/'

# hidden dimension of mlp
hid_dim = 64

def get_variables_spec(obs_dim, hid_dim, act_dim):
    """Get a list of variables.
    
     got from
     policy = load_zoo_agent('1', env, env_name, agent_id, None)
    policy.policy_obj.get_trainable_variables()
    """
    variables_spec = [
     ['retfilter/sum:0', []],
     ['retfilter/sumsq:0', []],
     ['retfilter/count:0', []],
     ['obsfilter/sum:0', [obs_dim]],
     ['obsfilter/sumsq:0', [obs_dim]],
     ['obsfilter/count:0', []],
     ['vff1/w:0', [obs_dim, hid_dim]],
     ['vff1/b:0', [hid_dim]],
     ['vff2/w:0', [hid_dim, hid_dim]],
     ['vff2/b:0', [hid_dim]],
     ['vfffinal/w:0', [hid_dim, 1]],
     ['vfffinal/b:0', [1]],
     ['pol1/w:0', [obs_dim, hid_dim]],
     ['pol1/b:0', [hid_dim]],
     ['pol2/w:0', [hid_dim, hid_dim]],
     ['pol2/b:0', [hid_dim]],
     ['polfinal/w:0', [hid_dim, act_dim]],
     ['polfinal/b:0', [act_dim]],
     ['logstd:0', [1, act_dim]]]
    return variables_spec

def get_variables(policy_unpickle, variables_spec):
    """Get variables as a dict."""

    # dict with variables
    variables = {name: np.zeros(shape) for name, shape in variables_spec}
    
    # filling in the variables
    idx = 0
    for n, _ in variables_spec:
        s = variables[n].size
        variables[n] = policy_unpickle[idx:idx+s].reshape(variables[n].shape)
        idx += s
    assert idx == len(policy_unpickle), "Wrong variables_spec expected=%d got=%d" % (idx, len(policy_unpickle))
    
    return variables

def normalizer_mean_std(variables, name):
    """Get RunningMeanStd mean/std parameters."""
    mean = 1. * variables[name + 'filter' + '/sum:0'] / variables[name + 'filter' + '/count:0']
    var_est = (1. * variables[name + 'filter' + '/sumsq:0'] / variables[name + 'filter' + '/count:0']) - np.square(mean)
    std = np.sqrt(np.maximum(var_est, 1e-2))
    return mean, std

def nets_to_weight_array(nets, check_content_id=False):
    """Convert networks from load_gym_compete back to a numpy array."""
    variables_spec = nets['variables_spec']
    variables_spec_dict = dict(variables_spec)
    variables = [x[0] for x in variables_spec]
    keep_vars = [x for x in variables if x.startswith('retfilter') or x.startswith('obsfilter')]
    variable_to_w = {}
    variable_to_w['logstd:0'] = nets['policy'].layers[5].get_weights()[0]
    
    for var in keep_vars:
        variable_to_w[var] = nets['variables'][var]
    nets_mapping = [('pol', nets['policy'], 2), ('vff', nets['value'], 1)]
    layer_names = ['1', '2', 'final']
    var_types = [('w', 0), ('b', 1)]

    for net_name, net, offset in nets_mapping:
        for layer_id, layer_name in enumerate(layer_names):
            weights = net.layers[offset + layer_id].get_weights()
            for var_type, var_offset in var_types:
                target_var = net_name + layer_name + '/' + var_type + ':0'
                var = weights[var_offset]
                target_shape = tuple(variables_spec_dict[target_var])
                origin_shape = var.shape
                assert target_shape == origin_shape
                #print(f"Target shape {target_shape} origin shape {origin_shape}")
                variable_to_w[target_var] = var

    for var, shape in nets['variables_spec']:
        assert variable_to_w[var].shape == tuple(shape)
        
    def compress_variables(variable_to_w, variables_spec):
        """Create a flat array from a dict of variables."""
        result = []
        for var, shape in variables_spec:
            assert variable_to_w[var].shape == tuple(shape)
            result += list(variable_to_w[var].flatten())
        return result
    
    vars_compressed = compress_variables(variable_to_w, variables_spec)
    vars_compressed = np.array(vars_compressed, dtype=nets['weights_unpickle'].dtype)
    
    # checking that the result is the same as original input
    if check_content_id:
        assert np.allclose(vars_compressed, nets['weights_unpickle'])

    return vars_compressed

def get_policy_value_nets(env_name, agent_id, pickle_path=pickle_path, variables_spec=None, version=1, load_weights=True):
    """Get networks from a pickle file."""
    results = {}
    
    env = gym.make(env_name)
    obs_dim = env.observation_space[0].shape[0]
    act_dim = env.action_space[0].shape[0]
    env_name_2 = env_name.split('/')[1]
    env.close()
    
    if isinstance(agent_id, int):
        agent_id += 1
    
    policy_unpickle = pickle.load(open(pickle_path + env_name_2 + '/agent%s_parameters-v%d.pkl' % (str(agent_id), version), 'rb'))
    if variables_spec is None:
        variables_spec = get_variables_spec(obs_dim=obs_dim, hid_dim=hid_dim, act_dim=act_dim)
    variables = get_variables(variables_spec=variables_spec, policy_unpickle=policy_unpickle)

    n_saved_weights = policy_unpickle.shape[0]

    #print('tf eager', tf.executing_eagerly())
    
    init = tf.keras.initializers.Orthogonal()
    
    def build_policy():
        model_policy_inp = keras.Input(shape=(obs_dim,))
        model_policy_y = ObservationPreprocessingLayer(*normalizer_mean_std(variables, 'obs'), 5)(model_policy_inp)
        model_policy_y = keras.layers.Dense(hid_dim, input_shape=(obs_dim,), activation='tanh', use_bias=True, kernel_initializer=init)(model_policy_y)
        model_policy_y = keras.layers.Dense(hid_dim, activation='tanh', use_bias=True, kernel_initializer=init)(model_policy_y)
        model_policy_mean = keras.layers.Dense(act_dim, activation=None, use_bias=True, name='mean', kernel_initializer=init)(model_policy_y)
        model_policy_mean = keras.layers.Reshape((act_dim, 1), name='reshape_mean')(model_policy_mean)

        model_policy_std = UnconnectedVariableLayer(name='std', shape=(act_dim,), initializer=tf.keras.initializers.Zeros())(model_policy_y)
        model_policy_std = keras.layers.Reshape((act_dim, 1), name='reshape_std')(model_policy_std)
        model_policy_mean_std_ = keras.layers.Concatenate(axis=2)([model_policy_mean, model_policy_std])
        model_policy_mean_std_flat_ = tf.keras.layers.Flatten(data_format='channels_first')(model_policy_mean_std_)
        model_policy_mean_std = keras.Model(inputs=model_policy_inp, outputs=model_policy_mean_std_)
        model_policy_mean_std_flat = keras.Model(inputs=model_policy_inp, outputs=model_policy_mean_std_flat_)

        model_policy_ = DiagonalNormalSamplingLayer()(model_policy_mean_std_)
        model_policy = keras.Model(inputs=model_policy_inp, outputs=model_policy_)

        model_policy(np.zeros((1, obs_dim), dtype=np.float32))
        #model_policy.summary()
        return model_policy_mean_std, model_policy_mean_std_flat, model_policy
    
    results['policy_mean_logstd'], results['policy_mean_logstd_flat'], results['policy'] = build_policy()
    
    def build_value():
        model_value = keras.Sequential([
            ObservationPreprocessingLayer(*normalizer_mean_std(variables, 'obs'), 5),
            keras.layers.Dense(hid_dim, input_shape=(obs_dim,), activation='tanh', use_bias=True, name='h1', kernel_initializer=init),
            keras.layers.Dense(hid_dim, activation='tanh', use_bias=True, name='h2', kernel_initializer=init),
            keras.layers.Dense(1, activation=None, use_bias=True, name='value', kernel_initializer=init),
            ValuePostprocessingLayer(*normalizer_mean_std(variables, 'ret'))
        ])

        model_value(np.zeros((1, obs_dim), dtype=np.float32))
        #model_value.summary()
        return model_value
    
    results['value'] = build_value()
    
    results['weights_unpickle'] = policy_unpickle
    results['variables'] = variables
    results['variables_spec'] = variables_spec
    
    # counting twice
    model_weights = results['policy'].count_params() + results['value'].count_params() - results['value'].layers[0].count_params()
    #print("Weights delta", n_saved_weights - model_weights)
    
    if load_weights:
        layer_names = ['1', '2', 'final']
        # name in old vars, new model, layer offset
        networks_map = [('vff', results['value'], 1), ('pol', results['policy'], 2)]

        for net_name, net, layer_offset in networks_map:
            for layer, layer_name in enumerate(layer_names):
                net.layers[layer_offset + layer].set_weights(
                    [variables[net_name + layer_name + '/' + p + ':0']
                     for p in ['w', 'b']])

        # setting LOGstd value
        results['policy'].layers[-5].set_weights([variables['logstd:0']])
    
    return results

def difference_new_networks(env_name, agent_id, model_value, model_policy_mean_logstd,
                      n_test_obs=1000, max_scale=100, eps=1e-10, verbose=True):
    """Test new vs old networks."""
    results = {}

    # loading old policy/value
    env = gym.make(env_name)
    env.num_envs = 1
    obs_dim = env.observation_space[0].shape[0]
    act_dim = env.action_space[0].shape[0]
    env_name_2 = env_name.split('/')[1]

    from aprl.envs.gym_compete import load_zoo_agent
    policy = load_zoo_agent('1', env, env_name, agent_id, None)
    mlp_policy = policy.policy_obj

    # random observations
    obs = np.random.randn(n_test_obs, obs_dim)
    obs = np.multiply(np.linspace(1, max_scale, n_test_obs)[:, np.newaxis], obs)

    # computing value
    value_new = model_value.predict(obs)
    value_old = np.array([mlp_policy.value(np.array([o])) for o in obs])
    
    def show_error(arr1, arr2, verbose=True):
        """Compare two arrays."""
        def get_delta(arr1, arr2):
            """Get relative error."""
            arr1f = arr1.flatten()
            arr2f = arr2.flatten()
            delta = 100 * np.abs(arr1f - arr2f) / (eps + np.abs(arr2f))
            return delta
        
        delta = get_delta(arr1, arr2)
        
        if verbose:
            print(np.max(delta))
            plt.hist(delta)
            plt.xlabel('Error %')
            plt.show()
        return {'max': np.max(delta), 'mean': np.mean(delta),
                'min': np.min(delta), 'median': np.median(delta)}
    
    results['value'] = show_error(value_new, value_old, verbose=verbose)

    p_new = model_policy_mean_logstd.predict(obs)
    p_old = np.array([mlp_policy.proba_step(np.array([o])) for o in obs])
    p_old = p_old[:, :, 0, :]
    p_old = np.swapaxes(p_old, 1, 2)

    p_old_mean = p_old[:, :, 0]
    p_new_mean = p_new[:, :, 0]
    results['policy_mean'] = show_error(p_new_mean, p_old_mean, verbose=verbose)

    p_old_var = np.exp(p_old[:, :, 1])
    p_new_var = p_new[:, :, 1]

    results['policy_std'] = show_error(p_new_var, p_old_var, verbose=verbose)
    
    return results
