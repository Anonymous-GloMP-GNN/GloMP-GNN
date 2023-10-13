from torch import nn
import torch
import dgl

from modules import (ResidualModuleWrapper, FeedForwardModule, GCNModule, SAGEModule, GATModule, GATSepModule,
                     TransformerAttentionModule, TransformerAttentionSepModule)
from modules import MaxwellDemonFilter

MODULES = {
    'ResNet': [FeedForwardModule],
    'GCN': [GCNModule],
    'SAGE': [SAGEModule],
    'GAT': [GATModule],
    'GAT-sep': [GATSepModule],
    'GT': [TransformerAttentionModule, FeedForwardModule],
    'GT-sep': [TransformerAttentionSepModule, FeedForwardModule]
}


NORMALIZATION = {
    'None': nn.Identity,
    'LayerNorm': nn.LayerNorm,
    'BatchNorm': nn.BatchNorm1d
}


class MyModel(nn.Module):
    def __init__(self, args, input_dim, hidden_dim,output_dim,hidden_dim_multiplier, num_heads,normalization, dropout,number_of_edges, num_layers):
        super(MyModel, self).__init__()
        self.args = args

        normalization = NORMALIZATION[normalization]
        # self.filter = MaxwellDemonFilter(hidden_dim, hidden_dim,  hidden_dim, hidden_dim_multiplier, num_heads,dropout,number_of_edges)
        self.input_linear = nn.Linear(in_features=input_dim, out_features=hidden_dim)
        self.residual_modules = nn.ModuleList()
        for _ in range(num_layers):
            residual_module = ResidualModuleWrapper(module=MaxwellDemonFilter,
                                                    normalization=normalization,
                                                    dim=hidden_dim,
                                                    hidden_dim_multiplier=hidden_dim_multiplier,
                                                    num_heads=num_heads,
                                                    dropout=dropout,
                                                    number_of_edges = number_of_edges,
                                                    args= self.args
                                                    )

            self.residual_modules.append(residual_module)

        self.dropout = nn.Dropout(p=dropout)
        self.act = nn.GELU()
        self.output_normalization = normalization(hidden_dim * (num_layers+1))
        self.output_linear = nn.Linear(in_features=hidden_dim * (num_layers+1), out_features=output_dim)
        # self.output_normalization = normalization(hidden_dim )
        # self.output_linear = nn.Linear(in_features=hidden_dim, out_features=output_dim)

    def forward(self,graph,x):
        x = self.input_linear(x)
        x = self.dropout(x)
        x = self.act(x) 
        x_all =  x
        for residual_module in self.residual_modules:
            x = residual_module(graph, x)
            x_all = torch.cat([x_all,x], dim = -1)

        x_all = self.output_normalization(x_all)
        x_all = self.output_linear(x_all).squeeze(1)
        # x = self.output_normalization(x)
        # x = self.output_linear(x).squeeze(1)

        return x_all
        # return x

