from torch import nn, zeros, FloatTensor, cat, cuda
import torch
from torch.autograd import Variable
import numpy as np

import config as cfg
from model.AttNet import AttNet
from model.CharNet import CharNet
from model.LMnet import LMnet
from model.utils import to_scalar, TimeDistributed


class SeqNet(nn.Module):
    def __init__(self, emb_mat, isCrossEnt=True, char_level="None", imp_feat='v1'):
        super().__init__()
        self.emb_mat_tensor = Variable(cuda.FloatTensor(emb_mat))
        assert self.emb_mat_tensor.size(1) == cfg.EMBEDDING_DIM
        self.vocab_size = self.emb_mat_tensor.size(0)
        self.emb_dim = self.emb_mat_tensor.size(1)
        self.hidden_size = cfg.LSTM_HIDDEN_SIZE
        self.batch_size = 1  # we can only do batch size 1.
        self.num_layers = 1
        self.num_dir = 2
        self.out_size = cfg.CATEGORIES
        self.pf_dim = cfg.PF_EMBEDDING_DIM
        self.char_emb_dim = cfg.EMBEDDING_DIM
        self.imp_feat = imp_feat
        self.char_level = char_level

        # init embedding layer, with pre-trained embedding matrix : emb_mat
        self.emb_lookup = Embedding(self.emb_mat_tensor)

        self.char_net = CharNet(cfg.CHAR_EMB_DIM, cfg.CHAR_RECURRENT_SIZE, out_size=cfg.EMBEDDING_DIM)

        self.att_net = AttNet(cfg.EMBEDDING_DIM, cfg.EMBEDDING_DIM, cfg.EMBEDDING_DIM)

        if self.char_level == "Input":
            self.lstm = nn.LSTM(input_size=self.emb_dim + self.char_emb_dim, batch_first=True,
                                num_layers=self.num_layers,
                                hidden_size=self.hidden_size,
                                bidirectional=True)

        elif self.char_level == "Attention":
            self.lstm = nn.LSTM(input_size=self.emb_dim, batch_first=True,
                                num_layers=self.num_layers,
                                hidden_size=self.hidden_size, bidirectional=True)

        else:
            self.lstm = nn.LSTM(input_size=self.emb_dim, batch_first=True,
                                num_layers=self.num_layers,
                                hidden_size=self.hidden_size,
                                bidirectional=True)

        if self.imp_feat == "v2":
            self.feat_net = None

        self.lm_forward = TimeDistributed(LMnet(input_size=self.hidden_size,
                                                out_size=min(self.vocab_size + 1, cfg.LM_MAX_VOCAB_SIZE),
                                                hidden_size=cfg.LM_HIDDEN_SIZE), batch_first=True)

        self.lm_backward = TimeDistributed(LMnet(input_size=self.hidden_size,
                                                 out_size=min(self.vocab_size + 1, cfg.LM_MAX_VOCAB_SIZE),
                                                 hidden_size=cfg.LM_HIDDEN_SIZE), batch_first=True)

        self.lstm_linear = nn.Linear(self.hidden_size * 2, cfg.LSTM_OUT_SIZE)

        if self.imp_feat == "v1":
            self.linear = nn.Linear(cfg.LSTM_OUT_SIZE + cfg.FEATURE_SIZE,
                                    self.out_size)
        else:
            self.linear = nn.Linear(cfg.LSTM_OUT_SIZE,
                                    self.out_size)



        # self.time_linear = TimeDistributed(self.linear, batch_first=True)
        self.hidden_state = self.init_state()
        if not isCrossEnt:
            self.log_softmax = nn.LogSoftmax()

        self.isCrossEnt = isCrossEnt

    def init_state(self):
        """Get cell states and hidden states."""
        h0_encoder_bi = Variable(zeros(self.num_layers * self.num_dir, self.batch_size, self.hidden_size))
        c0_encoder_bi = Variable(zeros(self.num_layers * self.num_dir, self.batch_size, self.hidden_size))

        self.hidden_state = (h0_encoder_bi.cuda(), c0_encoder_bi.cuda())

        self.char_net.init_state()

    def forward(self, sent_idx_seq, char_idx_seq, features):
        cfg.ver_print("Sent Index sequence", sent_idx_seq)

        seq_len = sent_idx_seq.size(1)

        emb = self.emb_lookup(sent_idx_seq)

        if self.char_level == "Input":
            char_emb = self.char_net(char_idx_seq)
            inp = cat([emb, char_emb], dim=2)

        elif self.char_level == "Attention":
            char_emb = self.char_net(char_idx_seq)
            inp = self.att_net(emb, char_emb)

        else:
            inp = emb

        # emb is now of size(1 x seq_len x EMB_DIM)
        cfg.ver_print("Embedding for the Sequence", inp)

        lstm_out, hidden_state = self.lstm(inp, self.hidden_state)
        # lstm_out is of size (1 x seq_len x 2*EMB_DIM)

        lstm_forward, lstm_backward = lstm_out[:, :, :cfg.LSTM_HIDDEN_SIZE], lstm_out[:, :, -cfg.LSTM_HIDDEN_SIZE:]

        # making sure that you got the correct lstm_forward and lstm_backward.
        assert to_scalar(torch.sum(lstm_forward[:, seq_len - 1, :] - hidden_state[0][0, :, :])) == 0
        assert to_scalar(torch.sum(lstm_backward[:, 0, :] - hidden_state[0][1, :, :])) == 0

        lm_f_out = self.lm_forward(lstm_forward[:, :-1, :])

        lm_b_out = self.lm_backward(lstm_backward[:, 1:, :])

        cfg.ver_print("Language Model Forward pass out", lm_f_out)
        cfg.ver_print("Language Model Backward pass out", lm_b_out)

        lstm_out = self.lstm_linear(lstm_out.squeeze())

        lstm_out = torch.sigmoid(lstm_out)

        lstm_out = lstm_out.unsqueeze(dim=0)

        if self.imp_feat == "v1":
            label_out = cat([lstm_out, features], dim=2)
        elif self.imp_feat == "v2":
            f_out = self.feat_net(features)
            label_out = cat([lstm_out, f_out])
        else:
            label_out = lstm_out

        linear_out = self.linear(label_out.view(seq_len, -1))
        if self.isCrossEnt:
            out = linear_out
        else:
            out = self.log_softmax(linear_out)

        cfg.ver_print("LINEAR OUT", linear_out)
        cfg.ver_print("FINAL OUT", out)

        if self.char_level == "Attention":
            return lm_f_out, lm_b_out, out, emb, char_emb
        else:
            return lm_f_out, lm_b_out, out


class Embedding(nn.Module):
    def __init__(self, emb_mat):
        super().__init__()
        self.emb_mat = emb_mat

    def forward(self, x):
        v_l = []
        cfg.ver_print("input to embedding layer", x)
        # x is of size (1 x seq_len)
        seq_len = x.size(1)

        n = x[0].cpu().data.numpy()

        for i in n.tolist():
            v = self.emb_mat[i]
            # v is of size (EMB_DIM)
            # cfg.ver_print("v", v)

            v_l.append(v)
        v = torch.stack(v_l, dim=0)
        # v is of size (seq_len x EMB_DIM)

        v = v.view(1, seq_len, -1)
        # v is now of size(1 x seq_len x EMB_DIM)

        # cfg.ver_print("Embedding out", v)

        return v
