import sys
import random

import pandas as pd
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.distributions import Categorical

filename = 'params.py'

feature_list = ["open", "high", "low", "close", "volume"]
transFee = 100
capital = 500000

hidden_size = 64
learning_rate = 1e-4
gamma = 0.99
lmbda = 0.95
clip = 0.1
ent = 1e-3
epoch = 2
nsteps = 200
neps = 100000


class TradingEnv:
    def __init__(self, dailyOhlcvFile):
        self.dailyOhlcv = pd.read_csv(dailyOhlcvFile)

        diff1 = self.dailyOhlcv[feature_list].values.astype(np.float32)
        diff1 = np.log(diff1[1:]) - np.log(diff1[:-1])
        self.diffOhlcv = np.vstack((diff1[0] * 0, diff1))

        self.reset()

    def reset(self, test=False):
        self.cur_capital = capital
        self.holding = 0
        self.pre_total = self.cur_capital

        if not test:
            # random start point
            self.cur_index = random.randint(2, nsteps)
        else:
            self.cur_index = 2

        return self._observation(self.cur_index)

    def step(self, action):
        cur_price, next_price = self.dailyOhlcv.loc[self.cur_index:self.cur_index+1, [
            "open"]].values.astype(np.float32)[:, 0]

        if action == 1 and self.cur_capital > transFee:
            self.holding = (self.cur_capital - transFee) / cur_price
            self.cur_capital = 0
        elif action == -1 and self.holding * cur_price > transFee:
            self.cur_capital = self.holding * cur_price - transFee
            self.holding = 0

        reward = self._reward(cur_price, next_price)
        info = {'capital': self.cur_capital,
                'holding': self.holding, 'return_rate': self.pre_total / capital - 1}

        done = (self.cur_index == len(self.dailyOhlcv) - 2)
        if not done:
            next_obs = self._observation(self.cur_index)
        else:
            next_obs = self.reset()

        self.cur_index += 1
        return next_obs, reward, done, info

    def _reward(self, cur_price, next_price):
        cur_total = self.cur_capital + self.holding * next_price
        reward =  (cur_total - self.pre_total) / capital * 100
        self.pre_total = cur_total
        return reward

    def _observation(self, today):
        return np.hstack((self.diffOhlcv[today-1], [self.diffOhlcv[today][0]]))


class PPO(nn.Module):
    def __init__(self, env=None):
        super(PPO, self).__init__()
        self.data = []

        self.fc1 = nn.Linear(len(feature_list) + 1, hidden_size)
        self.lstm = nn.LSTM(hidden_size, hidden_size)
        self.fc2 = nn.Linear(hidden_size, hidden_size)

        self.fc_pi = nn.Linear(hidden_size, 3)
        self.fc_v = nn.Linear(hidden_size, 1)

        self.optimizer = optim.Adam(self.parameters(), lr=learning_rate)

        self.device = torch.device(
            'cuda' if torch.cuda.is_available() else 'cpu')
        self.to(self.device)

        self.env = env

    def forward(self, x, hidden):
        x = F.relu(self.fc1(x))
        x = x.view(-1, 1, hidden_size)
        x, lstm_hidden = self.lstm(x, hidden)
        x = F.relu(self.fc2(x))

        pi = self.fc_pi(x)
        pi = F.softmax(pi, dim=2)
        v = self.fc_v(x)
        return pi, v, lstm_hidden

    def put_data(self, transition):
        self.data.append(transition)

    def make_batch(self):
        s_lst, a_lst, r_lst, s_next_lst, prob_a_lst, h_in_lst, h_out_lst, done_lst = [
        ], [], [], [], [], [], [], []
        for transition in self.data:
            s, a, r, s_next, prob_a, h_in, h_out, done = transition

            s_lst.append(s)
            a_lst.append([a])
            r_lst.append([r])
            s_next_lst.append(s_next)
            prob_a_lst.append([prob_a])
            h_in_lst.append(h_in)
            h_out_lst.append(h_out)
            done_mask = 0 if done else 1
            done_lst.append([done_mask])

        s, a, r, s_next, done_mask, prob_a = \
            torch.tensor(s_lst, dtype=torch.float32, device=self.device), \
            torch.tensor(a_lst, dtype=torch.int32, device=self.device), \
            torch.tensor(r_lst, dtype=torch.float32, device=self.device), \
            torch.tensor(s_next_lst, dtype=torch.float32, device=self.device), \
            torch.tensor(done_lst, dtype=torch.float32, device=self.device), \
            torch.tensor(prob_a_lst, dtype=torch.float32, device=self.device)
        self.data = []

        return s, a, r, s_next, done_mask, prob_a, h_in_lst[0], h_out_lst[0]

    def update_net(self):
        s, a, r, s_next, done_mask, prob_a, (h_in1,
                                             h_in2), (h_out1, h_out2) = self.make_batch()
        h_in = (h_in1.detach(), h_in2.detach())
        h_out = (h_out1.detach(), h_out2.detach())

        for i in range(epoch):
            # advantage
            pi, v_s, _ = self.forward(s, h_in)

            with torch.no_grad():
                _, v_s_next, _ = self.forward(s_next, h_out)
                td_target = r + gamma * v_s_next.squeeze(1) * done_mask
                delta = td_target - v_s.squeeze(1)

                advantage_lst = []
                advantage = 0.0
                for t in range(len(delta) - 1, -1, -1):
                    advantage = gamma * lmbda * advantage + delta[t]
                    advantage_lst.append(advantage)
                advantage_lst.reverse()
                advantage = torch.tensor(
                    advantage_lst, dtype=torch.float32, device=self.device)

                returns = v_s.flatten() + advantage

            # loss
            dist = Categorical(pi.squeeze(1))
            log_pi_a = dist.log_prob(a.squeeze(1))
            # a/b == exp(log(a)-log(b))
            ratio = torch.exp(log_pi_a - torch.log(prob_a.squeeze(1)))

            surr1 = ratio * advantage
            surr2 = torch.clamp(ratio, 1-clip, 1+clip) * advantage

            pi_loss = -torch.min(surr1, surr2).mean()
            v_loss = F.smooth_l1_loss(v_s.flatten(), returns).mean()
            ent_loss = ent * -dist.entropy().mean()
            loss = pi_loss + v_loss + ent_loss

            self.optimizer.zero_grad()
            loss.backward(retain_graph=True)
            self.optimizer.step()

            return {"pi_loss": pi_loss.item(), "v_loss": v_loss.item(), "ent_loss": ent_loss.item()}

    def train(self):
        best_return_rate = 0
        
        for n_epi in range(neps):
            h_out = (torch.zeros([1, 1, hidden_size], dtype=torch.float32, device=self.device),
                     torch.zeros([1, 1, hidden_size],  dtype=torch.float32, device=self.device))
            s = self.env.reset()
            done = False

            while not done:
                for t in range(nsteps):
                    h_in = h_out
                    prob, v, h_out = self.forward(torch.from_numpy(s).to(
                        dtype=torch.float32, device=self.device), h_in)
                    prob = prob.view(-1)

                    m = Categorical(prob)
                    a = m.sample().item()
                    s_next, r, done, info = env.step(a - 1)  # -1, 0, 1

                    self.put_data(
                        (s, a, r, s_next, prob[a].item(), h_in, h_out, done))
                    s = s_next

                    if done:
                        break

                loss_info = self.update_net()

            print(n_epi)
            print(loss_info)
            print(info)

            if info['return_rate'] > best_return_rate:
                best_return_rate = info['return_rate']
                save_to_file(filename, str(self.state_dict()))

    def test(self):
        from params import param_dict
        self.load_state_dict(param_dict)

        h_out = (torch.zeros([1, 1, hidden_size], dtype=torch.float32, device=self.device),
                 torch.zeros([1, 1, hidden_size],  dtype=torch.float32, device=self.device))
        s = self.env.reset(test=True)
        done = False

        while not done:
            h_in = h_out
            prob, v, h_out = self.forward(torch.from_numpy(s).to(
                dtype=torch.float32, device=self.device), h_in)
            prob = prob.view(-1)

            a = torch.argmax(prob).item()
            s_next, r, done, info = env.step(a - 1)  # -1, 0, 1

            s = s_next

        print(info)


def save_to_file(file_name, contents):
    fh = open(file_name, 'w')
    fh.write(
        "from collections import OrderedDict\nfrom torch import tensor\n\nparam_dict = ")
    fh.write(contents)
    fh.close()


if __name__ == "__main__":
    torch.set_printoptions(precision=7, threshold=10000000)

    dailyOhlcvFile = sys.argv[1]
    command = sys.argv[2]
    env = TradingEnv(dailyOhlcvFile)

    agent = PPO(env)
    if command == 'train':
        agent.train()
    elif command == 'test':
        agent.test()