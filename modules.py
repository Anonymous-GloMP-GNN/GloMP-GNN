import torch
from torch import nn
from dgl import ops
from dgl.nn.functional import edge_softmax
import torch.nn.functional as F

class ResidualModuleWrapper(nn.Module):
    def __init__(self, module, normalization, dim, **kwargs):
        super().__init__()
        self.normalization = normalization(dim)
        self.module = module(dim=dim, **kwargs)

    def forward(self, graph, x):
        x_res = self.normalization(x)
        x_res = self.module(graph, x_res)
        # x = x + x_res

        return x_res


class FeedForwardModule(nn.Module):
    def __init__(self, dim, hidden_dim_multiplier, dropout, input_dim_multiplier=1, **kwargs):
        super().__init__()
        input_dim = int(dim * input_dim_multiplier)
        hidden_dim = int(dim * hidden_dim_multiplier)
        self.linear_1 = nn.Linear(in_features=input_dim, out_features=hidden_dim)
        self.dropout_1 = nn.Dropout(p=dropout)
        self.act = nn.GELU()
        self.linear_2 = nn.Linear(in_features=hidden_dim, out_features=dim)
        self.dropout_2 = nn.Dropout(p=dropout)

    def forward(self, graph, x):
        x = self.linear_1(x)
        x = self.dropout_1(x)
        x = self.act(x)
        x = self.linear_2(x)
        x = self.dropout_2(x)

        return x



class MaxwellDemonFilter(nn.Module):
    def __init__(self, dim, hidden_dim_multiplier, num_heads,dropout,number_of_edges,args):
        super(MaxwellDemonFilter, self).__init__()
        self.fc = nn.Linear(dim, dim)

        _check_dim_and_num_heads_consistency(dim, num_heads)
        self.args = args
        self.dim = dim
        self.num_heads = num_heads
        self.head_dim = dim // num_heads
        self.fc_layer = nn.Linear(self.head_dim,1)
        self.input_linear = nn.Linear(in_features=dim, out_features=dim)
        self.number_of_edges = number_of_edges
        self.attn_linear_u = nn.Linear(in_features=dim, out_features=num_heads)
        self.attn_linear_v = nn.Linear(in_features=dim, out_features=num_heads, bias=False)
        self.attn_act = nn.LeakyReLU(negative_slope=0.2)
        self.filter_act = nn.ReLU()
        if self.args.use_combinations == True:
            self.input_dim_multiplier = 4
        else:
            self.input_dim_multiplier = 2
        self.feed_forward_module = FeedForwardModule(dim=dim,
                                                     input_dim_multiplier=self.input_dim_multiplier,
                                                     hidden_dim_multiplier=hidden_dim_multiplier,
                                                     dropout=dropout)
        # self.maxwell_filter_layer = nn.Linear(dim, self.number_of_edges)
        self.maxwell_filter_layer = nn.Linear(891 , self.number_of_edges * self.num_heads)
        # self.new_linear_layer = nn.Linear(2 * self.num_heads, self.num_heads)
        self.gate_weight = nn.Parameter(torch.Tensor(self.dim))
        self.gate_bias = nn.Parameter(torch.Tensor(self.dim))
        self.fc_laplace = nn.Linear(self.dim, self.dim)
        self.k =  nn.Parameter(torch.Tensor(1))
        # self.new_linear_layer = nn.Linear(2 * self.dim, self.dim)
        self.get_laplace = nn.Linear(self.dim, self.num_heads)
        self.chaos_factor = nn.Parameter(torch.Tensor(1))
        self.tanh = nn.Tanh()
        self.sim = nn.Linear(dim,dim)
    def get_laplace_demon_state(self, x):
        laplace = self.get_laplace(x)
        # # laplace,_ = torch.max(x,0)
        # laplace = laplace.reshape(1,-1) 
        # return 
        # 加入混沌扰动
        chaos_disturbance = torch.randn_like(laplace) * self.chaos_factor
        laplace += chaos_disturbance
        return laplace

    def normalize(self, x):
        min_val = x.min()
        max_val = x.max()
        normalized_x = (x - min_val) / (max_val - min_val)
        return normalized_x

    def cosine_similarity(self, x1, x2, dim=-1):
        return F.cosine_similarity(x1, x2, dim=dim)
    def compute_energy(self, x, graph):
        laplace_demon_state = self.get_laplace_demon_state(x)
        
        # 计算每个节点邻居的平均Laplace Demon状态
        neighbor_average = ops.copy_u_mean(graph, laplace_demon_state)
        
        # 能量函数：节点的Laplace Demon状态与其邻居的差异
        energy = laplace_demon_state - neighbor_average
        
        return energy

    def forward(self, graph,x):
        x = self.fc(x)

        attn_scores_u = self.attn_linear_u(x)
        attn_scores_v = self.attn_linear_v(x)

        attn_scores = ops.u_add_v(graph, attn_scores_u, attn_scores_v)
        attn_scores = self.attn_act(attn_scores)
        attn_probs = edge_softmax(graph, attn_scores)

        if self.args.use_filters == False:
            pass
        else:
            src,dst = graph.edges()
            src = src.to(dtype=torch.long)
            dst = dst.to(dtype=torch.long)
            energy = self.compute_energy(x, graph)
            filter = torch.sigmoid(energy[dst]-energy[src])
            attn_probs = attn_probs * filter


        x = x.reshape(-1, self.head_dim, self.num_heads)
        message = ops.u_mul_e_sum(graph, x, attn_probs)
        if self.args.use_combinations == True:
            message_2 = ops.copy_u_mean(graph, x)
            x = x.reshape(-1, self.dim)
            message = message.reshape(-1, self.dim)
            message_2 = message_2.reshape(-1, self.dim)      
            degrees = graph.out_degrees().float()
            degree_edge_products = ops.u_mul_v(graph, degrees, degrees)
            norm_coefs = 1 / degree_edge_products ** 0.5
            message_3 = ops.u_mul_e_sum(graph, x, norm_coefs)
            message_3 = message_3.reshape(-1, self.dim)
            x = torch.cat([x,message,message_2,message_3], axis=1)
        else:
            x = x.reshape(-1, self.dim)
            message = message.reshape(-1, self.dim)
            x = torch.cat([x,message], axis=1)

        x = self.feed_forward_module(graph, x)

        return x
