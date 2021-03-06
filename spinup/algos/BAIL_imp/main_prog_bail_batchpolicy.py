import gym
import numpy as np
import torch
import argparse
import os

from spinup.utils.logx import EpochLogger
from spinup.utils.run_utils import setup_logger_kwargs
from spinup.algos.BAIL_imp import utils, bail_training_batchpolicy

# check directory
if os.getcwd().find('lanya') == -1:
	os.chdir("/gpfsnyu/scratch/xc1305")
print('data directory', os.getcwd())
# check pytorch device
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print("running on device:", device)

def bail_learn(algo = 'bail_2_bah',
			   env_set="Hopper-v2", seed=0, buffer_type='FinalSigma0.5_env_0_1000K',
			   gamma=0.99, ue_rollout=1000, augment_mc='gain', C=None,
			   eval_freq=100, max_timesteps=int(25e4), batch_size=1000,
			   lr=1e-3, wd=0, ue_lr=3e-3, ue_wd=2e-2, ue_loss_k=1000, ue_vali_freq=1250,
			   pct_anneal_type='constant', last_pct=0.25,
			   pct_info_dic={},
			   select_type='border', save_freq=int(1e2),
			   logger_kwargs=dict()):

	"""set up logger"""
	global logger
	logger = EpochLogger(**logger_kwargs)
	logger.save_config(locals())

	if not os.path.exists("./plots"):
		os.makedirs("./plots")
	if not os.path.exists("./pytorch_models"):
		os.makedirs("./pytorch_models")

	file_name = "%s_%s_%s" % (algo, env_set, seed)
	setting_name = "%s_r%s_g%s" % (buffer_type.replace('env', env_set), ue_rollout, gamma)
	setting_name += '_noaug' if not (augment_mc) else ''
	setting_name += '_augNew' if augment_mc == 'new' else ''

	print("---------------------------------------")
	print("Algo: " + file_name + "\tData: " + buffer_type)
	print("Settings: " + setting_name)
	print("Evaluate Policy every", eval_freq * batch_size * 0.8 / 1e6,
		  'epoches; Total', max_timesteps * batch_size * 0.8 / 1e6, 'epoches')
	print("---------------------------------------")

	env = gym.make(env_set)
	test_env = gym.make(env_set)

	# Set seeds
	env.seed(seed)
	test_env.seed(seed)
	env.action_space.np_random.seed(seed)
	test_env.action_space.np_random.seed(seed)
	torch.manual_seed(seed)
	np.random.seed(seed)

	state_dim = env.observation_space.shape[0]
	action_dim = env.action_space.shape[0]
	max_action = float(env.action_space.high[0])

	# Load buffer
	if 'sac' in buffer_type:
		replay_buffer = utils.BEAR_ReplayBuffer()
		desire_stop_dict = {'Hopper-v2': 1000, 'Walker2d-v2': 500, 'HalfCheetah-v2': 4000, 'Ant-v2': 750}
		buffer_name = buffer_type.replace('env', env_set).replace('crt', str(desire_stop_dict[env_set]))
		replay_buffer.load(buffer_name)
		buffer_name += '_1000K'
		setting_name = setting_name.replace('crt', str(desire_stop_dict[env_set]))
	elif 'Final' in buffer_type or 'sigma' in buffer_type:
		replay_buffer = utils.ReplayBuffer()
		buffer_name = buffer_type.replace('env', env_set)
		replay_buffer.load(buffer_name)
	elif 'optimal' in buffer_type:
		buffer_name = buffer_type.replace('env', env_set)
		setting_name = buffer_name
		setting_name += 'noaug' if not (augment_mc) else ''
		replay_buffer = utils.ReplayBuffer()
		replay_buffer.load(buffer_name)
	else:
		raise FileNotFoundError('! Unknown type of dataset %s' % buffer_type)

	# Load data for training UE
	states = np.load('./results/ueMC_%s_S.npy' % buffer_name, allow_pickle=True).squeeze()

	setting_name += '_Gain' if augment_mc == 'gain' else '_Gt'
	gts = np.load('./results/ueMC_%s.npy' % setting_name, allow_pickle=True).squeeze()
	print('Load mc returns type', augment_mc, 'with gamma:', gamma, 'rollout length:', ue_rollout)

	# Start training
	print('-- Policy train starts --')
	# Initialize policy
	if algo == 'bail_2_bah':
		policy = bail_training_batchpolicy.BAIL_selebah(state_dim, action_dim, max_action, max_iters=max_timesteps, States=states, MCrets=gts,
								ue_lr=ue_lr, ue_wd=ue_wd,
								pct_anneal_type=pct_anneal_type, last_pct=last_pct, pct_info_dic=pct_info_dic,
								select_type=select_type, C=C)
	else:
		raise Exception("! undefined BAIL implementation '%s'" % algo)

	training_iters, epoch = 0, 0
	
	while training_iters < max_timesteps:
		epoch += eval_freq * batch_size * 0.8 / 1e6
		bail_state_dict = policy.train(replay_buffer, training_iters, iterations=eval_freq, batch_size=batch_size,
								ue_loss_k=ue_loss_k,  ue_vali_freq=ue_vali_freq,
								logger=logger)

		avgtest_reward = evaluate_policy(policy, test_env)
		training_iters += eval_freq

		if (training_iters % save_freq == 0):
			print(training_iters)
			logger.save_state(bail_state_dict, training_iters)

		# log training info
		logger.log_tabular('Epoch', epoch)
		logger.log_tabular('AverageTestEpRet', avgtest_reward)
		logger.log_tabular('TotalSteps', training_iters)
		logger.log_tabular('CloneLoss', average_only=True)
		logger.log_tabular('UELoss', average_only=True)
		logger.log_tabular('BatchUEtrnSize', average_only=True)
		logger.log_tabular('SVal', with_min_and_max=True)
		logger.log_tabular('SelePct', average_only=True)
		logger.log_tabular('BatchUpSize', with_min_and_max=True)
		logger.log_tabular('UEValiLossMin', average_only=True)
		if select_type == 'border':
			logger.log_tabular('Border', with_min_and_max=True)
		elif select_type == 'margin':
			logger.log_tabular('Margin', with_min_and_max=True)
		else:
			raise Exception('! undefined selection type')

		logger.dump_tabular()



# Runs policy for X episodes and returns average reward
def evaluate_policy(policy, env, eval_episodes=10):
	tol_reward = 0
	for _ in range(eval_episodes):
		obs = env.reset()
		done = False
		while not done:
			action = policy.select_action(np.array(obs))
			obs, reward, done, _ = env.step(action)
			tol_reward += reward

	avg_reward = tol_reward / eval_episodes

	print ("---------------------------------------")
	print ("Evaluation over %d episodes: %f" % (eval_episodes, avg_reward))
	print ("---------------------------------------")
	return avg_reward


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("--env_set", default="Hopper-v2")  # OpenAI gym environment name
	parser.add_argument("--seed", default=1, type=int)  # Sets Gym, PyTorch and Numpy seeds
	parser.add_argument("--eval_freq", default=int(1e2), type=int)  # How often (time steps) we evaluate
	parser.add_argument("--ue_vali_freq", default=int(1e2), type=int)
	parser.add_argument("--max_timesteps", default=int(4e2), type=int)  # Max time steps to run environment for
	parser.add_argument('--exp_name', type=str, default='bail_prog_local')
	args = parser.parse_args()

	logger_kwargs = setup_logger_kwargs(args.exp_name, args.seed)

	bail_learn(env_set=args.env_set, seed=args.seed,
			   eval_freq=args.eval_freq, ue_vali_freq=args.ue_vali_freq,
			   max_timesteps=args.max_timesteps,
			   logger_kwargs=logger_kwargs)
