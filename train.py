import argparse
from tqdm import tqdm
from torch.nn import CrossEntropyLoss
import torch.nn.functional as F
import torch
from torch.cuda.amp import autocast, GradScaler
from GCNII import GCNII
from model import Model, MyModel
from FAGCN import FAGCN
from GPRGNN import GPRGNN
from datasets import Dataset
from utils import Logger, get_parameter_groups, get_lr_scheduler_with_warmup,get_adj
import networkx as nx

def get_args():
    parser = argparse.ArgumentParser()

    parser.add_argument('--name', type=str, default=None, help='Experiment name. If None, model name is used.')
    parser.add_argument('--save_dir', type=str, default='experiments', help='Base directory for saving information.')
    parser.add_argument('--dataset', type=str, default='minesweeper',
                        choices=['roman-empire', 'amazon-ratings', 'minesweeper', 'tolokers', 'questions',
                                 'squirrel', 'squirrel-directed', 'squirrel-filtered', 'squirrel-filtered-directed',
                                 'chameleon', 'chameleon-directed', 'chameleon-filtered', 'chameleon-filtered-directed',
                                 'actor', 'texas', 'texas-4-classes', 'cornell', 'wisconsin','cora','citeseer','pubmed'])

    # model architecture
    parser.add_argument('--model', type=str, default='my_model',
                        choices=['ResNet', 'GCN', 'SAGE', 'GAT', 'GAT-sep', 'GT', 'GT-sep','my_model','my_model_no_node','FAGCN','GCNII','GPRGNN'])
    parser.add_argument('--num_layers', type=int, default=32)
    parser.add_argument('--hidden_dim', type=int, default=128)
    parser.add_argument('--hidden_dim_multiplier', type=float, default=1)
    parser.add_argument('--num_heads', type=int, default=8)
    parser.add_argument('--normalization', type=str, default='LayerNorm', choices=['None', 'LayerNorm', 'BatchNorm'])

    # regularization
    parser.add_argument('--dropout', type=float, default=0.2)
    parser.add_argument('--weight_decay', type=float, default=5e-06)

    # training parameters
    parser.add_argument('--lr', type=float, default=1e-2)
    parser.add_argument('--num_steps', type=int, default=1000)
    parser.add_argument('--num_warmup_steps', type=int, default=None,
                        help='If None, warmup_proportion is used instead.')
    parser.add_argument('--warmup_proportion', type=float, default=0, help='Only used if num_warmup_steps is None.')

    # node feature augmentation
    parser.add_argument('--use_sgc_features', default=False, action='store_true')
    parser.add_argument('--use_identity_features', default=False, action='store_true')
    parser.add_argument('--use_adjacency_features', default=False, action='store_true')
    parser.add_argument('--do_not_use_original_features', default=False, action='store_true')

    parser.add_argument('--use_filters', default=True, action='store_false')
    parser.add_argument('--use_combinations', default=True, action='store_false')
    
    parser.add_argument('--num_runs', type=int, default=10)
    parser.add_argument('--device', type=str, default='cuda:3')
    parser.add_argument('--amp', default=False, action='store_true')
    parser.add_argument('--verbose', default=False, action='store_true')

    args = parser.parse_args()

    if args.name is None:
        args.name = args.model

    return args


def train_step(args, model, dataset, optimizer, scheduler, scaler, amp=False):
    model.train()

    with autocast(enabled=amp):
        if args.model == 'GCNII':
            logits = model(dataset.node_features, dataset.adj)
            logits = logits[:,0]

        else:
            logits = model(dataset.graph, dataset.node_features)
        loss = dataset.loss_fn(input=logits[dataset.train_idx], target=dataset.labels[dataset.train_idx])

    scaler.scale(loss).backward()
    scaler.step(optimizer)
    scaler.update()
    optimizer.zero_grad()
    scheduler.step()


@torch.no_grad()
def evaluate(args, model, dataset, amp=False):
    model.eval()

    with autocast(enabled=amp):
        if args.model == 'GCNII':
            logits = model(dataset.node_features, dataset.adj)
            logits = logits[:,0]
        else:
            logits = model(dataset.graph, dataset.node_features)
    metrics = dataset.compute_metrics(logits)

    return metrics


def main():
    args = get_args()
    torch.manual_seed(0)

    dataset = Dataset(name=args.dataset,
                      add_self_loops=(args.model in ['GCN', 'GAT', 'GT']),
                      device=args.device,
                      use_sgc_features=args.use_sgc_features,
                      use_identity_features=args.use_identity_features,
                      use_adjacency_features=args.use_adjacency_features,
                      do_not_use_original_features=args.do_not_use_original_features)

    logger = Logger(args, metric=dataset.metric, num_data_splits=dataset.num_data_splits)




    for run in range(1, args.num_runs + 1):

        if args.model == 'my_model':
            #######                                        
            dataset.graph.add_nodes(1)
            dataset.graph.add_edges(len(dataset.node_features),range(len(dataset.node_features)))
            l_x,_ = torch.max(dataset.node_features,0)
            # l_x = torch.mean(dataset.node_features,0)        
            l_x = l_x.unsqueeze(1).T
            dataset.node_features = torch.cat((dataset.node_features,l_x),0)
            #####
            model = MyModel(
                input_dim=dataset.num_node_features,
                hidden_dim=args.hidden_dim,
                output_dim=dataset.num_targets,
                hidden_dim_multiplier=args.hidden_dim_multiplier,
                num_heads=args.num_heads,
                normalization=args.normalization,
                dropout=args.dropout, 
                number_of_edges = dataset.graph.number_of_edges(),
                num_layers=args.num_layers,
                args = args
                )

        elif args.model =='my_model_no_node':
            model = MyModel(args,
                input_dim=dataset.num_node_features,
                hidden_dim=args.hidden_dim,
                output_dim=dataset.num_targets,
                hidden_dim_multiplier=args.hidden_dim_multiplier,
                num_heads=args.num_heads,
                normalization=args.normalization,
                dropout=args.dropout, 
                number_of_edges = dataset.graph.number_of_edges(),
                num_layers=args.num_layers,
                )
        elif args.model =='FAGCN':
            g = dataset.graph.to(args.device)
            deg = g.in_degrees().to(args.device).float().clamp(min=1)
            norm = torch.pow(deg, -0.5)
            g.ndata['d'] = norm
            model = FAGCN(g, dataset.num_node_features, args.hidden_dim, dataset.num_targets, args.dropout, 0.3, args.num_layers).to(args.device)
        elif args.model == 'GCNII':
            model = GCNII(nfeat=dataset.num_node_features,
                nlayers=args.num_layers,
                nhidden=args.hidden_dim,
                nclass=dataset.num_targets + 1,
                dropout=args.dropout,
                lamda = 0.5, 
                alpha=0.1,
                variant= False).to(args.device)
        elif args.model == 'GPRGNN':
            model = GPRGNN(dataset.num_node_features, args.hidden_dim, dataset.num_targets,args.device)
        else:
            model = Model(model_name=args.model,
                          num_layers=args.num_layers,
                          input_dim=dataset.num_node_features,
                          hidden_dim=args.hidden_dim,
                          output_dim=dataset.num_targets,
                          hidden_dim_multiplier=args.hidden_dim_multiplier,
                          num_heads=args.num_heads,
                          normalization=args.normalization,
                          dropout=args.dropout)


        model.to(args.device)

        parameter_groups = get_parameter_groups(model)
        optimizer = torch.optim.AdamW(parameter_groups, lr=args.lr, weight_decay=args.weight_decay)
        scaler = GradScaler(enabled=args.amp)
        scheduler = get_lr_scheduler_with_warmup(optimizer=optimizer, num_warmup_steps=args.num_warmup_steps,
                                                 num_steps=args.num_steps, warmup_proportion=args.warmup_proportion)





        logger.start_run(run=run, data_split=dataset.cur_data_split + 1)
        with tqdm(total=args.num_steps, desc=f'Run {run}', disable=args.verbose) as progress_bar:
            for step in range(1, args.num_steps + 1):
                train_step(args, model=model, dataset=dataset, optimizer=optimizer, scheduler=scheduler,
                           scaler=scaler, amp=args.amp)
                metrics = evaluate(args, model=model, dataset=dataset, amp=args.amp)
                logger.update_metrics(metrics=metrics, step=step)

                progress_bar.update()
                progress_bar.set_postfix({metric: f'{value:.2f}' for metric, value in metrics.items()})

        logger.finish_run()
        model.cpu()
        dataset.next_data_split()

    logger.print_metrics_summary()


if __name__ == '__main__':
    main()
