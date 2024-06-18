import gym
from gym import spaces
import numpy as np
import pandas as pd
import torch
from torch.nn import CrossEntropyLoss, NLLLoss

from utils.stimuli import create_block_stimuli
from utils.vec_env import VecEnv


class IBLSession(gym.Env):

    def __init__(self,
                 base_loss_fn,
                 num_stimulus_strength,
                 min_stimulus_strength,
                 max_stimulus_strength,
                 block_side_probs,
                 trials_per_block_param,
                 blocks_per_session,
                 min_trials_per_block,
                 max_trials_per_block,
                 max_obs_per_trial,
                 time_delay_penalty,
                 rnn_steps_before_obs,
                 **kwargs):

        # TODO: are these necessary
        self.action_space = spaces.Discrete(2)  # left or right
        self.observation_space = spaces.Box(low=-np.inf, high=np.inf, shape=(1,))
        self.reward_range = (0, 1)

        # parameters defining environment (session) behavior
        self.num_stimulus_strength = num_stimulus_strength
        self.min_stimulus_strength = min_stimulus_strength
        self.max_stimulus_strength = max_stimulus_strength
        self.block_side_probs = block_side_probs
        self.trials_per_block_param = trials_per_block_param
        self.blocks_per_session = blocks_per_session
        self.min_trials_per_block = min_trials_per_block
        self.max_trials_per_block = max_trials_per_block
        self.rnn_steps_before_obs = rnn_steps_before_obs
        self.max_obs_per_trial = max_obs_per_trial
        self.time_delay_penalty = time_delay_penalty
        self.possible_trial_strengths = tuple(np.linspace(
            min_stimulus_strength, max_stimulus_strength, num_stimulus_strength))
        self.possible_trial_strengths_probs = tuple(np.ones(
            num_stimulus_strength) / num_stimulus_strength)
        self.max_rnn_steps_per_trial = rnn_steps_before_obs + max_obs_per_trial
        self.loss_fn = self.create_loss_fn(base_loss_fn=base_loss_fn)
        self.reward_fn = self.create_reward_fn()

        # to (re)initialize the following variables, call self.reset()
        self.num_trials_per_block = None
        self.session_data = None
        self.stimuli = None
        self.trial_strengths = None
        self.trial_sides = None
        self.block_sides = None
        self.losses = None
        self.current_block_within_session = None
        self.current_trial_within_session = None
        self.current_trial_within_block = None
        self.current_rnn_step_within_trial = None
        self.current_rnn_step_within_session = None
        self.total_num_rnn_steps = None
        self.trial_start_flag = False
        self.trial_end_flag = False

    def reset(self):
        """
        (Re)initializes session.

        Previously, we relied on the fact that the RNN had a fixed number of
        steps per trial to preallocate the entire session, and then iterated
        over the preallocated values. This is no longer the case because the
        model can now decide to act faster or slower.
        """

        self.num_trials_per_block = self.create_num_trials_per_block()
        max_rnn_steps_per_session = np.sum(self.num_trials_per_block) * self.max_rnn_steps_per_trial
        self.session_data = self.create_session_data(max_rnn_steps_per_session)
        self.losses = torch.zeros(max_rnn_steps_per_session)

        self.stimuli, self.trial_strengths, self.trial_sides, self.block_sides = \
            self.create_stimuli()

        self.current_trial_within_session = 0
        self.current_trial_within_block = 0
        self.current_block_within_session = 0
        self.current_rnn_step_within_trial = 0
        self.current_rnn_step_within_session = 0
        self.total_num_rnn_steps = 0

        # create first observation with shape (2,)
        stimulus = self.stimuli[self.current_block_within_session][
            self.current_trial_within_block, self.current_rnn_step_within_trial]

        # record start of trial, block
        self.session_data.at[0, 'trial_start'] = 1.
        self.session_data.at[0, 'block_start'] = 1.

        step_output = dict(
            stimulus=stimulus.reshape((1, -1)),  # shape (1 rnn step, 2)
            reward=torch.zeros(1).requires_grad_(True),
            loss=torch.zeros(1).requires_grad_(True),
            info=None,
            done=True if self.current_block_within_session == self.blocks_per_session else False,
            trial_index=self.current_trial_within_session,)

        return step_output

    def step(self,
             model_prob_output,
             model_logit_output,
             model_hidden,
             model):
        """
        :param model_prob_output: shape (time step=1, 2)
        :param model_logit_output: shape (time step=1, 2)
        :param model_hidden: shape (num layers, model hidden size)
        :return:
        """


        left_action_prob = model_prob_output[0, 0].item()
        right_action_prob = model_prob_output[0, 1].item()

        if self.current_trial_within_session == 0:
            left_action_prob = 1 if model_prob_output[0, 0] > 0.5 else 0
            right_action_prob = 1 if model_prob_output[0, 1] > 0.5 else 0

        correct_action = self.trial_sides[self.current_block_within_session][
            self.current_trial_within_block, self.current_rnn_step_within_trial]
        correct_action_index = (1 + correct_action) // 2
        correct_action_prob = left_action_prob if correct_action.item() == -1 else right_action_prob

        # if RNN or GRU, shape = (number of layers, hidden state size)
        # if LSTM, shape = (num layers, hidden state size, 2)
        # where 2 corresponds to c, t
        hidden_state = model_hidden.detach().numpy()

        # record data
        left_stimulus = self.stimuli[self.current_block_within_session][
            self.current_trial_within_block, self.current_rnn_step_within_trial][0].item()
        right_stimulus = self.stimuli[self.current_block_within_session][
            self.current_trial_within_block, self.current_rnn_step_within_trial][1].item()
        trial_strength = self.trial_strengths[self.current_block_within_session][
            self.current_trial_within_block, self.current_rnn_step_within_trial].item()
        trial_side = self.trial_sides[self.current_block_within_session][
            self.current_trial_within_block, self.current_rnn_step_within_trial].item()
        block_side = self.block_sides[self.current_block_within_session][
            self.current_trial_within_block, self.current_rnn_step_within_trial].item()

        # target has shape (batch=1,). Reshape to (1, 1) to match action with shape
        # (batch = 1, 1) for loss function
        is_blank_rnn_step = left_stimulus == 0 and right_stimulus == 0
        loss = self.loss_fn(
            target=correct_action_index.reshape((1,)).long(),
            action_probs=model_prob_output,
            # action_probs=model_logit_output,
            is_blank_rnn_step=is_blank_rnn_step)  # * self.current_rnn_step_within_trial
        self.losses[self.current_rnn_step_within_session] = loss

        is_timeout = (self.current_rnn_step_within_trial + 1) == self.max_rnn_steps_per_trial
        reward = self.reward_fn(
            target=correct_action_index,
            input=model_prob_output,
            is_timeout=is_timeout,
            is_blank_rnn_step=is_blank_rnn_step)

        timestep_data = dict(
            trial_within_session=self.current_trial_within_session,
            block_index=self.current_block_within_session,
            trial_index=self.current_trial_within_block,
            rnn_step_index=self.current_rnn_step_within_trial,
            rnn_step_within_session=self.current_rnn_step_within_session,
            left_stimulus=left_stimulus,
            right_stimulus=right_stimulus,
            trial_strength=trial_strength,
            trial_side=trial_side,
            block_side=block_side,
            loss=loss.item(),
            reward=reward.item(),
            left_action_prob=left_action_prob,
            right_action_prob=right_action_prob,
            correct_action_prob=correct_action_prob,
            hidden_state=hidden_state)

        for column, value in timestep_data.items():
            self.session_data.at[self.current_rnn_step_within_session, column] = value

        # increment counters
        # current rnn step counter always advances. need for truncation
        self.current_rnn_step_within_session += 1

        # advance current rnn step within trial.
        self.current_rnn_step_within_trial += 1

        # move to next trial if either (i) maxed out number of rnn_steps within trial
        # or model made an action i.e. receive a reward/punishment
        if abs(reward.item()) > 0.9:

            # record whether action was taken, which side, and whether it was correct
            if left_action_prob > 0.9 or right_action_prob > 0.9:
                self.session_data.at[self.current_rnn_step_within_session - 1,
                                     'action_taken'] = 1.
                self.session_data.at[self.current_rnn_step_within_session - 1,
                                     'correct_action_taken'] = reward == 1.
                self.session_data.at[self.current_rnn_step_within_session - 1,
                                     'action_side'] = -1. if left_action_prob > 0.9 else 1.
            else:
                self.session_data.at[self.current_rnn_step_within_session - 1,
                                     'action_taken'] = 0.
                self.session_data.at[self.current_rnn_step_within_session - 1,
                                     'correct_action_taken'] = 0.

            self.current_trial_within_session += 1

            # store that trial is over, new trial has begun
            self.session_data.at[self.current_rnn_step_within_session - 1, 'trial_end'] = 1.
            self.session_data.at[self.current_rnn_step_within_session, 'trial_start'] = 1.

            self.current_rnn_step_within_trial = 0
            self.current_trial_within_block += 1

            # move to next block if finished trials within block
            if self.current_trial_within_block == self.num_trials_per_block[self.current_block_within_session]:

                # store that block is over, new block has begun
                self.session_data.at[self.current_rnn_step_within_session - 1, 'block_end'] = 1.
                self.session_data.at[self.current_rnn_step_within_session, 'block_end'] = 1.

                self.current_block_within_session += 1
                self.current_rnn_step_within_trial = 0
                self.current_trial_within_block = 0

        if self.current_block_within_session == self.blocks_per_session:
            done = True
            self.current_block_within_session = 0
        else:
            done = False

        stimulus = self.stimuli[self.current_block_within_session][
            self.current_trial_within_block, self.current_rnn_step_within_trial]

        # store any additional desired information
        info = dict()

        # loss is used by the optimizer
        # reward is (possibly different) input to the model at the next time step
        step_output = dict(
            loss=loss,
            stimulus=stimulus.reshape((1, -1)),
            reward=reward,
            info=info,
            done=done,
            trial_index=self.current_trial_within_block,)

        return step_output

    def create_loss_fn(self,
                       base_loss_fn):
        """

        :return: loss_fn:   must have two keyword arguments, target and input
                            target.shape should be (batch size = 1, )
                            target.type should be long (i.e. integer)
                            input.shape should be (batch size = 1, num actions = 2,)
        """

        def loss_fn(target, action_probs, is_blank_rnn_step):
            if is_blank_rnn_step:
                loss = torch.zeros(1, dtype=torch.float32, requires_grad=False)[0]
            else:
                # TODO: adding time delay penalty is currently pointless
                loss = base_loss_fn(target=target, input=action_probs)
            return loss

        return loss_fn

    def create_reward_fn(self):
        """
        :return: reward_fn:   must have two keyword arguments, target and input
                    target.shape should be (batch size = 1,)
                    input.shape should be (batch size = 1, num actions = 2,)
        """

        def reward_fn(target, input, is_timeout, is_blank_rnn_step):

            max_prob, max_prob_idx = torch.max(input, dim=1)

            if is_blank_rnn_step:
                reward = torch.zeros(1, dtype=torch.float32)
            elif max_prob > 0.9:
                # for an action to be rewarded, the model must have made the correct choice
                # also, punish model if action was incorrect
                reward = 2. * (target == max_prob_idx) - 1.
            elif is_timeout:
                # punish model for timing out
                reward = torch.zeros(1, dtype=torch.float32).fill_(-1)
            else:
                # give 0
                # reward = torch.zeros(1)
                reward = torch.zeros(1, dtype=torch.float32).fill_(self.time_delay_penalty)

            return reward

        return reward_fn

    def create_num_trials_per_block(self):
        # sample number of trials per block, ensuring values are between
        # [min_trials_per_block, max_trials_per_block], resampling otherwise
        # use rejection sampling
        num_trials_per_block = []
        while len(num_trials_per_block) < self.blocks_per_session:
            sample = np.random.geometric(p=self.trials_per_block_param)
            if self.min_trials_per_block <= sample <= self.max_trials_per_block:
                num_trials_per_block.append(sample)
        return num_trials_per_block

    def create_session_data(self, max_rnn_steps_per_session):
        # create Pandas DataFrame for tracking all session data
        index = np.arange(max_rnn_steps_per_session)
        columns = ['rnn_step_within_session',
                   'trial_within_session',
                   'block_index',
                   'trial_index',
                   'rnn_step_index',
                   'block_side',
                   'trial_side',
                   'trial_strength',
                   'trial_start',
                   'trial_end',
                   'block_start',
                   'block_end',
                   'left_stimulus',
                   'right_stimulus',
                   'loss',
                   'reward',
                   'correct_action_prob',
                   'left_action_prob',
                   'right_action_prob',
                   'hidden_state',
                   'action_taken',
                   'action_side',
                   'correct_action_taken']

        session_data = pd.DataFrame(
            np.nan,  # initialize all to nan
            index=index,
            columns=columns,
            dtype=np.float16)

        # enable storing hidden states in the dataframe.
        # need to make the column have type object to doing so possible
        session_data.hidden_state = session_data.hidden_state.astype(object)

        return session_data

    def create_stimuli(self):

        # choose first block bias with 50-50 probability
        current_block_side = np.random.choice([0, 1])
        # each of stimuli, trial_stimulus_side, block_side will have
        # the following structure:
        #       list of length blocks_per_session
        #       each list element will be a torch tensor with shape
        #           (trials_per_session, max_rnn_steps_per_trial, 2)
        stimuli, trial_strengths = [], []
        trial_sides, block_sides = [], []
        for num_trials in self.num_trials_per_block:
            stimulus_creator_output = create_block_stimuli(
                num_trials=num_trials,
                block_side_bias_probabilities=self.block_side_probs[current_block_side],
                possible_trial_strengths=self.possible_trial_strengths,
                possible_trial_strengths_probs=self.possible_trial_strengths_probs,
                max_rnn_steps_per_trial=self.max_rnn_steps_per_trial)
            stimuli.append(torch.from_numpy(stimulus_creator_output['stimuli']))
            trial_strengths.append(torch.from_numpy(stimulus_creator_output['stimuli_strengths']))
            trial_sides.append(torch.from_numpy(stimulus_creator_output['trial_sides']))
            block_side = np.full(
                shape=(num_trials, self.max_rnn_steps_per_trial),
                fill_value=-1 if current_block_side == 0 else 1)
            block_sides.append(torch.from_numpy(block_side))

            current_block_side = 1 if current_block_side == 0 else 0

        # zero out stimuli for first rnn_steps_before_stimulus
        for block_stimuli in stimuli:
            block_stimuli[:, :2, :] = 0

        return stimuli, trial_strengths, trial_sides, block_sides

    def close(self, session_index):

        # add an indicator of which dataframe corresponds to which environment
        self.session_data['session_index'] = session_index

        # truncate unused rows
        self.session_data.drop(
            np.arange(self.current_rnn_step_within_session, len(self.session_data)),
            inplace=True)


def create_biased_choice_worlds(env_params,
                                base_loss_fn):
    """
    "biased choice world during which visual stimuli have to be actively moved
    by the mouse; left and right stimuli are presented with different probability
    in blocks of trials."
    """

    sessions = VecEnv(
        create_env_fn=create_session(base_loss_fn=base_loss_fn,
                                     kwargs=env_params['kwargs']),
        num_env=env_params['num_sessions'])

    return sessions


def create_session(base_loss_fn, kwargs):
    def _f():
        return IBLSession(base_loss_fn=base_loss_fn, **kwargs)
    return _f
