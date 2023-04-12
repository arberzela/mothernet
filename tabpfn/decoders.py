import torch
from torch import nn
import random


class ScaledDecoder(nn.Module):
    def __init__(self, ninp, nhid, nout):
        super().__init__()
        self.linear = nn.Linear(ninp, nhid)
        self.linear1 = nn.Linear(nhid, nout)
        self.linear2 = nn.Linear(nhid, 10)

    def forward(self, x):
        #return torch.cat([self.linear1(x), self.linear2(x)], -1)
        x = self.linear(x)
        x = nn.GELU()(x)
        temps = self.linear2(x).softmax(-1) @ torch.tensor([1.,1.4,1.7,2.,5.,10.,20.,40.,80.,160.], device=x.device)
        if random.random() > .99:
            print(temps.shape,temps[:,:2])
        return self.linear1(x) / temps.unsqueeze(-1)

class FixedScaledDecoder(nn.Module):
    def __init__(self, ninp, nhid, nout):
        super().__init__()
        self.mapper = nn.Sequential(nn.Linear(ninp, nhid), nn.GELU(), nn.Linear(nhid, nout))
        self.T = nn.Parameter(torch.ones(10000)/10000)

    def forward(self, x):
        return self.mapper(x)/self.T.sum()

class LinearModelDecoder(nn.Module):
    def __init__(self, emsize=512, nout=10, hidden_size=1024):
        super().__init__()
        self.emsize = emsize
        self.nout = nout
        self.hidden_size = hidden_size

        self.mlp = nn.Sequential(nn.Linear(emsize,  hidden_size),
                                 nn.ReLU(),
                                 nn.Linear(hidden_size, (emsize + 1) * nout))

    def forward(self, x):
        return self.mlp(x.mean(0)).reshape(-1, self.emsize + 1, self.nout)


class MLPModelDecoder(nn.Module):
    def __init__(self, emsize=512, nout=10, hidden_size=1024):
        super().__init__()
        self.emsize = emsize
        self.nout = nout
        self.hidden_size = hidden_size

        self.mlp = nn.Sequential(nn.Linear(emsize,  hidden_size),
                                 nn.ReLU(),
                                 nn.Linear(hidden_size, (emsize + 1) * nout + emsize ** 2 + emsize))

    def forward(self, x):
        if x.shape[0] != 0:
            res = self.mlp(x.mean(0))
        else:
            res = torch.zeros((self.emsize + 1) * self.nout + self.emsize ** 2 + self.emsize, device=x.device)
        emsize = self.emsize
        w2 = res[:, :self.nout * emsize].reshape(-1, emsize, self.nout)
        b2 = res[:, self.nout * emsize: self.nout * (emsize + 1)].reshape(-1, self.nout)
        w1 = res[:, self.nout * (emsize + 1): self.nout * (emsize + 1) + emsize ** 2].reshape(-1, emsize, emsize)
        b1 = res[:, -emsize:].reshape(-1, emsize)
        return b1, w1, b2, w2
