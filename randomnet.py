import torch
import torch.nn as nn
import torch.nn.functional as F

import networkx as nx

from layer import Node


class RandomNetwork(nn.Module):
    def __init__(self, in_planes, planes, G, downsample=True):
        '''Random DAG network of nodes

        Args:
            planes (int): number of channels each nodes have
            G (`networkx.classes.digraph.DiGraph`) : DAG from random graph generator
            downsample (bool): overrides downsample setting of the top layer
        '''
        super(RandomNetwork, self).__init__()
        self.nxgraph = G
        self.in_degree = G.in_degree
        self.pred = G.pred
        
        out_degree = G.out_degree
        self.bottom_layer = []
        self.nodes = nn.ModuleList()
        for nxnode in G.nodes:
            # Top layer nodes
            if self.in_degree(nxnode) == 0:
                node = Node(in_planes, planes, self.in_degree(nxnode),
                        downsample=downsample)
            else:
                node = Node(planes, planes, self.in_degree(nxnode))

            # Bottom layer nodes
            if out_degree(nxnode) == 0:
                self.bottom_layer.append(nxnode)

            self.nodes.append(node)

        # Nodes are sorted in topological order (edge start nodes fisrt)
        self.nxorder = [n for n in nx.topological_sort(self.nxgraph)]

        # Count live variable to reduce the memory usage
        ispans = [] # indices from ordered list stored in topological order
        succ = G.succ
        for nxnode in self.nxorder:
            nextnodes = [self.nxorder.index(n) for n in succ[nxnode]]
            span = max(nextnodes) if len(nextnodes) != 0 else G.number_of_nodes()
            ispans.append(span)

        self.live = [None for _ in self.nxorder] # list of nodeids in topological order stored in topological order
        for order, nxnode in enumerate(self.nxorder):
            self.live[order] = [inode for inode, ispan in enumerate(ispans) \
                    if ispan >= order and inode < order]
        # maximum #live-vars = max([len(nodes) for nodes in self.live])

    def forward(self, x):
        '''

        TODO do parallel processing of uncorrelated nodes (using compiler techniques)

        Args:
            x: A Tensor with size (N, C, H, W)

        Returns:
            A Tensor with size (N, C, H, W)
        '''
        # Traversal in the sorted graph
        outs = []
        for order, nxnode in enumerate(self.nxorder):
            node = self.nodes[nxnode]
            to_delete = []
            # Top layer nodes receive data from the upper layer
            if self.in_degree(nxnode) == 0:
                out = node(x.unsqueeze(-1)) # (N,Cin,H,W,F=1)
            else:
                y = []
                for p in self.pred[nxnode]:
                    ipred = self.nxorder.index(p)
                    iout = self.live[order].index(ipred)
                    y.append(outs[iout])
                    if order is not len(self.nxorder) - 1 and \
                            ipred not in self.live[order + 1]:
                        to_delete.append(iout)
                y = torch.stack(y) # (F,N,Cin,H,W)
                y = y.permute(1, 2, 3, 4, 0) # (N,Cin,H,W,F)
                out = node(y)

            # Make output layer compact
            if len(to_delete) is not 0:
                # Delete element in backwards in order to maintain consistency
                to_delete.sort(reverse=True)
                for i in to_delete:
                    del outs[i]
            outs.append(out)
        #  outs = [outs[i] for i in self.bottom_layer]

        # Aggregation on the output node
        out = torch.stack(outs) # (F,N,Cin,H,W)
        out = torch.mean(out, 0) # (N,Cin,H,W)
        return out


def test():
    from graph import GraphGenerator
    graphgen = GraphGenerator('WS', { 'K': 6, 'P': 0.25, })
    G = graphgen.generate(nnode=32)
    randnet = RandomNetwork(in_planes=3, planes=32, G=G, downsample=False)
    #  return
    x = torch.randn(32, 3, 224, 224)
    out = randnet(x)
    print(out.size())


if __name__ == '__main__':
    test()