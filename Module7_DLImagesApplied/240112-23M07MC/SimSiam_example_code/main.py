import argparse
import math
import os

import pandas as pd
import torch
import torch.nn.functional as F
from thop import profile, clever_format
from torch.optim import SGD
from torch.optim.lr_scheduler import LambdaLR
from torch.utils.data.dataloader import DataLoader
from tqdm import tqdm

from model import Model
from utils import CIFAR10Pair, test_transform, train_transform


# train for one epoch to learn features
def train(net, data_loader, train_optimizer):
    net.train()
    total_loss, total_num, train_bar = 0.0, 0, tqdm(data_loader, dynamic_ncols=True)
    for pos_1, pos_2, _ in train_bar:

        pos_1, pos_2 = pos_1.cuda(non_blocking=True), pos_2.cuda(non_blocking=True)

        feature_1, proj_1 = net(pos_1)
        feature_2, proj_2 = net(pos_2)

        # compute loss
        sim_1 = -(F.normalize(proj_1, dim=-1) * F.normalize(feature_2.detach(), dim=-1)).sum(dim=-1).mean()
        sim_2 = -(F.normalize(proj_2, dim=-1) * F.normalize(feature_1.detach(), dim=-1)).sum(dim=-1).mean()

        loss = 0.5 * sim_1 + 0.5 * sim_2

        train_optimizer.zero_grad()
        loss.backward()
        train_optimizer.step()

        total_num += pos_1.size(0)
        total_loss += loss.item() * pos_1.size(0)
        if epochs % 50 == 0:
            print('Train Epoch: [{}/{}] Loss: {:.4f}'.format(epoch, epochs, total_loss / total_num))

    return total_loss / total_num


# test for one epoch, use weighted knn to find the most similar images' label to assign the test image
def test(net, memory_data_loader, test_data_loader):
    net.eval()
    total_top1, total_top5, total_num, feature_bank = 0.0, 0.0, 0, []
    with torch.no_grad():
        # generate feature bank
        for data, _, _ in tqdm(memory_data_loader, desc='Feature extracting', dynamic_ncols=True):
            feature, proj = net(data.cuda(non_blocking=True))
            feature_bank.append(F.normalize(feature, dim=-1))
        # [D, N]
        feature_bank = torch.cat(feature_bank, dim=0).t().contiguous()
        # [N]
        feature_labels = torch.tensor(memory_data_loader.dataset.targets, device=feature_bank.device)

        # loop test data to predict the label by weighted knn search
        test_bar = tqdm(test_data_loader, dynamic_ncols=True)

        for data, _, target in test_bar:
            data, target = data.cuda(non_blocking=True), target.cuda(non_blocking=True)
            feature, proj = net(data)

            total_num += data.size(0)

            # compute cos similarity between each feature vector and feature bank ---> [B, N]
            sim_matrix = torch.mm(F.normalize(feature, dim=-1), feature_bank)
            # [B, K]
            sim_weight, sim_indices = sim_matrix.topk(k=k, dim=-1)
            # [B, K]
            sim_labels = torch.gather(feature_labels.expand(data.size(0), -1), dim=-1, index=sim_indices)
            sim_weight = sim_weight.exp()

            # counts for each class
            one_hot_label = torch.zeros(data.size(0) * k, c, device=sim_labels.device)
            # [B*K, C]
            one_hot_label = one_hot_label.scatter(dim=-1, index=sim_labels.view(-1, 1), value=1.0)
            # weighted score ---> [B, C]
            pred_scores = torch.sum(one_hot_label.view(data.size(0), -1, c) * sim_weight.unsqueeze(dim=-1), dim=1)

            pred_labels = pred_scores.argsort(dim=-1, descending=True)
            total_top1 += torch.sum((pred_labels[:, :1] == target.unsqueeze(dim=-1)).any(dim=-1).float()).item()
            total_top5 += torch.sum((pred_labels[:, :5] == target.unsqueeze(dim=-1)).any(dim=-1).float()).item()
            print('Test Epoch: [{}/{}] Acc@1:{:.2f}% Acc@5:{:.2f}%'
                                     .format(epoch, epochs, total_top1 / total_num * 100, total_top5 / total_num * 100))

    return total_top1 / total_num * 100, total_top5 / total_num * 100


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Train SimSiam')
    parser.add_argument('--feature_dim', default=2048, type=int, help='Feature dim for out vector')
    parser.add_argument('--k', default=200, type=int, help='Top k most similar images used to predict the label')
    parser.add_argument('--batch_size', default=512, type=int, help='Number of images in each mini-batch')
    parser.add_argument('--epochs', default=800, type=int, help='Number of sweeps over the dataset to train')

    # args parse
    args = parser.parse_args()
    feature_dim, k, batch_size, epochs = args.feature_dim, args.k, args.batch_size, args.epochs

    # data prepare
    train_data = CIFAR10Pair(root='data', train=True, transform=train_transform, download=True)
    train_loader = DataLoader(train_data, batch_size=batch_size, shuffle=True, num_workers=16, pin_memory=True)
    memory_data = CIFAR10Pair(root='data', train=True, transform=test_transform, download=True)
    memory_loader = DataLoader(memory_data, batch_size=batch_size, shuffle=False, num_workers=16, pin_memory=True)
    test_data = CIFAR10Pair(root='data', train=False, transform=test_transform, download=True)
    test_loader = DataLoader(test_data, batch_size=batch_size, shuffle=False, num_workers=16, pin_memory=True)

    # model setup and optimizer config
    model = Model(feature_dim).cuda()

    flops, params = profile(model, inputs=(torch.randn(1, 3, 32, 32).cuda(),), verbose=False)
    flops, params = clever_format([flops, params])

    print('# Model Params: {} FLOPs: {}'.format(params, flops))
    optimizer = SGD(model.parameters(), lr=0.03, momentum=0.9, weight_decay=5e-4)
    lr_scheduler = LambdaLR(optimizer, lr_lambda=lambda i: 0.5 * (math.cos(i * math.pi / epochs) + 1))
    c = len(memory_data.classes)

    results = {'train_loss': [], 'test_acc@1': [], 'test_acc@5': []}
    save_name_pre = '{}_{}_{}_{}'.format(feature_dim, k, batch_size, epochs)
    if not os.path.exists('results'):
        os.mkdir('results')
    best_acc = 0.0
    # training loop
    for epoch in range(1, epochs + 1):
        train_loss = train(model, train_loader, optimizer)

        results['train_loss'].append(train_loss)
        lr_scheduler.step()
        test_acc_1, test_acc_5 = test(model, memory_loader, test_loader)

        results['test_acc@1'].append(test_acc_1)
        results['test_acc@5'].append(test_acc_5)
        # save statistics
        data_frame = pd.DataFrame(data=results, index=range(1, epoch + 1))
        data_frame.to_csv('results/{}_statistics.csv'.format(save_name_pre), index_label='epoch')
        if test_acc_1 > best_acc:
            best_acc = test_acc_1
            torch.save(model.state_dict(), 'results/{}_model.pth'.format(save_name_pre))
