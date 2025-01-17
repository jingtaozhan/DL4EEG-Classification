import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_geometric.nn import global_add_pool, SGConv
from torch_scatter import scatter_add
from torch_geometric.nn import MessagePassing
import numpy as np
import mne
import copy
import json
import math
from config import total_channles

def get_edge_weight(dataset, mode = 'rgnn'):    
    global total_channles
    total_part = total_channles[dataset]
    montage = mne.channels.read_dig_fif('mode/' + 'montage.fif')
    montage.ch_names = json.load(open('mode/' + "montage_ch_names.json"))
    edge_pos = montage.get_positions()['ch_pos']
    edge_weight = np.zeros([len(total_part),len(total_part)])
    edge_pos_value = [edge_pos[key] for key in total_part]
    delta = 4710000000
    edge_index = [[],[]]
    if mode == 'dgcnn':
        for i in range(len(total_part)):
            for j in range(len(total_part)):
                edge_index[0].append(i)
                edge_index[1].append(j)
                if i == j:
                    edge_weight[i][j] = 1
                else:
                    edge_weight[i][j] = np.sum([(edge_pos_value[i][k] - edge_pos_value[j][k])**2 for k in range(3)])
                    if delta/edge_weight[i][j] > 1:
                        edge_weight[i][j] = math.exp(-edge_weight[i][j]/2)
                    else:
                        edge_weight[i][j] = 0
    elif mode == 'rgnn':
        for i in range(len(total_part)):
            for j in range(len(total_part)):
                edge_index[0].append(i)
                edge_index[1].append(j)
                if i == j:
                    edge_weight[i][j] = 1
                else:
                    edge_weight[i][j] = np.sum([(edge_pos_value[i][k] - edge_pos_value[j][k])**2 for k in range(3)])
                    edge_weight[i][j] = min(1, delta/edge_weight[i][j])
        global_connections = [['FP1','FP2'],['AF3','AF4'],['F5','F6'],['FC5','FC6'],['C5','C6'],['CP5','CP6'],['P5','P6'],['PO5','PO6'],['O1','O2']]

        for item in global_connections:
            if i in total_part and j in total_part:
                i = total_part.index(item[0])
                j = total_part.index(item[1])
                edge_weight[i][j] -= 1
                edge_weight[j][i] -= 1
    return edge_index, edge_weight 

def maybe_num_nodes(index, num_nodes=None):
    return index.max().item() + 1 if num_nodes is None else num_nodes


def add_remaining_self_loops(edge_index,
                             edge_weight=None,
                             fill_value=1,
                             num_nodes=None):
    num_nodes = maybe_num_nodes(edge_index, num_nodes)
    row, col = edge_index

    mask = row != col

    inv_mask = ~mask
    # print("inv_mask", inv_mask)
    
    loop_weight = torch.full(
        (num_nodes,),
        fill_value,
        dtype=None if edge_weight is None else edge_weight.dtype,
        device=edge_index.device)

    if edge_weight is not None:
        assert edge_weight.numel() == edge_index.size(1)
        remaining_edge_weight = edge_weight[inv_mask]

        if remaining_edge_weight.numel() > 0:
            loop_weight[row[inv_mask]] = remaining_edge_weight
        
        edge_weight = torch.cat([edge_weight[mask], loop_weight], dim=0)
    loop_index = torch.arange(0, num_nodes, dtype=row.dtype, device=row.device)
    loop_index = loop_index.unsqueeze(0).repeat(2, 1)
    edge_index = torch.cat([edge_index[:, mask], loop_index], dim=1)

    return edge_index, edge_weight


class NewSGConv(SGConv):
    def __init__(self, num_features, num_classes, K=1, cached=False,
                 bias=True):
        super(NewSGConv, self).__init__(num_features, num_classes, K=K, cached=cached, bias=bias)

    # allow negative edge weights
    @staticmethod
    def norm(edge_index, num_nodes, edge_weight, improved=False, dtype=None):
        if edge_weight is None:
            edge_weight = torch.ones((edge_index.size(1), ),
                                     dtype=dtype,
                                     device=edge_index.device)

        fill_value = 1 if not improved else 2
        edge_index, edge_weight = add_remaining_self_loops(
            edge_index, edge_weight, fill_value, num_nodes)
        row, col = edge_index

        deg = scatter_add(torch.abs(edge_weight), row, dim=0, dim_size=num_nodes)
        deg_inv_sqrt = deg.pow(-0.5)
        deg_inv_sqrt[deg_inv_sqrt == float('inf')] = 0
        return edge_index, deg_inv_sqrt[row] * edge_weight * deg_inv_sqrt[col]

    def forward(self, x, edge_index, edge_weight=None):
        """"""
        if not self.cached or self.cached_result is None:
            edge_index, norm = NewSGConv.norm(
                edge_index, x.size(0), edge_weight, dtype=x.dtype,)

            for k in range(self.K):
                x = self.propagate(edge_index, x=x, norm=norm)
            self.cached_result = x

        return self.lin(self.cached_result)

    def message(self, x_j, norm):
        return norm.view(-1, 1) * x_j

class RGNN(torch.nn.Module):
    def __init__(self, args, input_dim, num_nodes, device):
        super(RGNN, self).__init__()
        self.input_dim = input_dim
        self.num_nodes = num_nodes
        self.xs, self.ys = torch.tril_indices(self.num_nodes, self.num_nodes, offset=0)
        edge_index, edge_weight = get_edge_weight(args.dataset)
        self.edge_index = torch.tensor(edge_index)
        edge_weight = edge_weight.reshape(self.num_nodes, self.num_nodes)[self.xs, self.ys] # strict lower triangular values
        self.edge_weight = nn.Parameter(torch.Tensor(edge_weight).float(), requires_grad=True)
        self.device = device
        # 0.1 original paper
        self.dropout = 0.7
        self.conv1 = NewSGConv(num_features=self.input_dim, num_classes=400, K=2)
        self.fc = nn.Linear(400, 2)

    def append(self, edge_index, batch_size):
        edge_index_all = torch.LongTensor(2, edge_index.shape[1] * batch_size)
        data_batch = torch.LongTensor(self.num_nodes * batch_size)
        for i in range((batch_size)):
            edge_index_all[:,i*edge_index.shape[1]:(i+1)*edge_index.shape[1]] = edge_index + i * self.num_nodes
            data_batch[i*self.num_nodes:(i+1)*self.num_nodes] = i
        return edge_index_all.to(self.device), data_batch.to(self.device)

    def forward(self, X, X2, padding_masks):
        batch_size = len(X)
        x = X.view(-1, X.shape[-1])
        edge_index, data_batch = self.append(self.edge_index, batch_size)
        edge_weight = torch.zeros((self.num_nodes, self.num_nodes), device=edge_index.device)
        edge_weight[self.xs.to(edge_weight.device), self.ys.to(edge_weight.device)] = self.edge_weight
        edge_weight = edge_weight + edge_weight.transpose(1,0) - torch.diag(edge_weight.diagonal()) 
        edge_weight = edge_weight.reshape(-1).repeat(batch_size)
        x = F.relu(self.conv1(x, edge_index, edge_weight))
        x = global_add_pool(x, data_batch, size=batch_size)
        x = F.dropout(x, p=self.dropout, training=self.training)
        x = self.fc(x)
        return x
