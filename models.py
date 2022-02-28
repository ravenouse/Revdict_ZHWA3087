import math

import torch
import torch.nn as nn
import torch.nn.functional as F

import data

device = torch.device("cuda") if torch.cuda.is_available() else "cpu",
print("device",device)

class PositionalEncoding(nn.Module):
    """From PyTorch"""

    def __init__(self, d_model, dropout=0.1, max_len=4096):
        super(PositionalEncoding, self).__init__()
        self.dropout = nn.Dropout(p=dropout)
        pe = torch.zeros(max_len, d_model)
        position = torch.arange(0, max_len, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model)
        )
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0).transpose(0, 1)
        self.register_buffer("pe", pe)

    def forward(self, x):
        x = x + self.pe[: x.size(0)]
        return self.dropout(x)


class DefmodModel(nn.Module):
    """A transformer architecture for Definition Modeling."""

    def __init__(
        self, vocab, d_model=256, n_head=4, n_layers=4, dropout=0.3, maxlen=256
    ):
        super(DefmodModel, self).__init__()
        self.d_model = d_model
        self.padding_idx = vocab[data.PAD]
        self.eos_idx = vocab[data.EOS]
        self.maxlen = maxlen

        self.embedding = nn.Embedding(len(vocab), d_model, padding_idx=self.padding_idx)
        self.positional_encoding = PositionalEncoding(
            d_model, dropout=dropout, max_len=maxlen
        )
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=n_head, dropout=dropout, dim_feedforward=d_model * 2
        )
        self.transformer_encoder = nn.TransformerEncoder(
            encoder_layer, num_layers=n_layers
        )
        self.v_proj = nn.Linear(d_model, len(vocab))
        # initializing weights
        for name, param in self.named_parameters():
            if param.dim() > 1:
                nn.init.xavier_uniform_(param)
            elif "bias" in name:
                nn.init.zeros_(param)
            else:  # gain parameters of the layer norm
                nn.init.ones_(param)

    def generate_square_subsequent_mask(self, sz):
        "from Pytorch"
        mask = (torch.triu(torch.ones(sz, sz)) == 1).transpose(0, 1)
        mask = (
            mask.float()
            .masked_fill(mask == 0, float("-inf"))
            .masked_fill(mask == 1, float(0.0))
        )
        return mask

    def forward(self, vector, input_sequence=None):
        device = next(self.parameters()).device
        embs = self.embedding(input_sequence)
        seq = torch.cat([vector.unsqueeze(0), embs], dim=0)
        src = self.positional_encoding(seq)
        src_mask = self.generate_square_subsequent_mask(src.size(0)).to(device)
        src_key_padding_mask = torch.cat(
            [
                torch.tensor([[False] * input_sequence.size(1)]).to(device),
                (input_sequence == self.padding_idx),
            ],
            dim=0,
        ).t()
        transformer_output = self.transformer_encoder(
            src, mask=src_mask, src_key_padding_mask=src_key_padding_mask
        )
        v_dist = self.v_proj(transformer_output)
        return v_dist

    @staticmethod
    def load(file):
        return torch.load(file)

    def save(self, file):
        torch.save(self, file)

    def pred(self, vector):
        generated_symbols = []
        device = next(self.parameters()).device
        batch_size = vector.size(0)
        src = vector.unsqueeze(0)
        has_stopped = torch.tensor([False] * batch_size).to(device)
        src_key_padding_mask = torch.tensor([[False] * batch_size]).to(device)
        for step_idx in range(self.maxlen):
            src_mask = self.generate_square_subsequent_mask(src.size(0)).to(device)
            src_pe = self.positional_encoding(src)
            transformer_output = self.transformer_encoder(
                src_pe, mask=src_mask, src_key_padding_mask=src_key_padding_mask.t()
            )[-1]
            v_dist = self.v_proj(transformer_output)
            new_symbol = v_dist.argmax(-1)
            new_symbol = new_symbol.masked_fill(has_stopped, self.padding_idx)
            generated_symbols.append(new_symbol)
            src_key_padding_mask = torch.cat(
                [src_key_padding_mask, has_stopped.unsqueeze(0)], dim=0
            )
            has_stopped = has_stopped | (new_symbol == self.eos_idx)
            src = torch.cat([src, self.embedding(new_symbol).unsqueeze(0)], dim=0)
            if has_stopped.all():
                break
        output_sequence = torch.stack(generated_symbols, dim=0)
        return output_sequence


class RevdictModel(nn.Module):
    """A Bidirectional LSTM Rnn version for Reverse dictionary Modeling."""

    def __init__(
            self, vocab, d_model=256, n_layers=4, dropout=0.3, hidden_dim=256
    ):
        super(RevdictModel, self).__init__()
        self.n_layers = n_layers
        self.d_model = d_model
        self.padding_idx = vocab[data.PAD]
        self.eos_idx = vocab[data.EOS]
        self.hidden_dim = hidden_dim
        self.embedding = nn.Embedding(len(vocab), d_model, padding_idx=self.padding_idx)
        self.lstm = nn.LSTM(d_model,hidden_dim,n_layers,dropout=dropout,batch_first=False,bidirectional=True)
        self.dropout = nn.Dropout(dropout)
        self.e_proj = nn.Linear(2*hidden_dim,d_model)
        self.hidden = None

    def init_hidden(self,batch_size):
        return (torch.ones(2*self.n_layers,batch_size,self.hidden_dim).float().to(torch.device('cuda')),
                torch.ones(2*self.n_layers,batch_size,self.hidden_dim).float().to(torch.device('cuda')))

    def forward(self, gloss_tensor):
        key_padding_mask = gloss_tensor == self.padding_idx
        # print('key',key_padding_mask.shape)
        batch_size = gloss_tensor.size(1)
        # print("batch_size",batch_size)
        self.hidden = self.init_hidden(batch_size)
        # print("hidden",type(self.hidden))
        embs = self.embedding(gloss_tensor).float()
        # print('embs',embs.shape)
        hidden = self.hidden
        lstm_output, hidden = self.lstm(embs,hidden)
        lstm_output = self.dropout(lstm_output)
        # print('lstm_ouput',lstm_output.shape)
        summed_embs = lstm_output.masked_fill(
            key_padding_mask.unsqueeze(-1), 0
        ).sum(dim=0)
        # print("sum",summed_embs.shape)
        return self.e_proj(F.relu(summed_embs))



        return

    @staticmethod
    def load(file):
        return torch.load(file)

    def save(self, file):
        torch.save(self, file)
