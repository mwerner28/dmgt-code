import torch 
import pdb
from torch import nn, optim
from torch.utils.data import DataLoader, TensorDataset, Subset, random_split
import torchvision 
from torchvision.models.resnet import ResNet, Bottleneck
from torchvision.datasets import MNIST
from torchvision import transforms
from torchvision.utils import save_image
from torchvision.transforms import Compose, ToTensor, Normalize, Resize, ToPILImage
import numpy as np
import random
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import math
import os
from os.path import exists as file_exists
import seaborn as sns
import pandas as pd
import sympy
from sympy.solvers import solve 
from sympy import Symbol 
import argparse
from datetime import datetime
from sklearn.isotonic import IsotonicRegression

now = datetime.now()    
date_time = now.strftime("%m.%d_%H.%M.%S")

class MnistResNet(ResNet):
    def __init__(self):
        super(MnistResNet, self).__init__(Bottleneck, [3, 4, 6, 3], num_classes=10)
        self.conv1 = torch.nn.Conv2d(1, 64, 
            kernel_size=7, 
            stride=2, 
            padding=3, bias=False)

def class_card(prev_labels,
               x,
               num_classes,
               model,
               is_isoreg,
               rare_isoreg,
               common_isoreg,
               device):
    
    model.eval()
    logits = model(x.unsqueeze(0).to(device))
    softmax = nn.functional.softmax(logits, dim=1)
    top_score = softmax.max(1).values
    pred = softmax.max(1).indices
    
    if is_isoreg:
        if pred < 5:
            cal_top_score = rare_isoreg.predict(top_score.cpu().detach().numpy())
        else:
            cal_top_score = common_isoreg.predict(top_score.cpu().detach().numpy())
    
        renorm_factor = (1 - torch.from_numpy(cal_top_score).to(device))/(softmax.sum() - top_score) if top_score < 1 else 0
        softmax = torch.mul(renorm_factor*torch.ones(num_classes).to(device), softmax)
        softmax.squeeze().double()[pred] = torch.from_numpy(cal_top_score).to(device)
    
    softmax = softmax.squeeze()
    label_counts = [(prev_labels==i).float().sum() for i in range(num_classes)]
     
    return sum([softmax[i] * np.sqrt(label_counts[i] + 1) for i in range(num_classes)]), sum([softmax[i] * np.sqrt(label_counts[i]) for i in range(num_classes)])

def get_DIST_DMGT_subsets(stream_x,
                          stream_y,
                          taus,
                          sel_round,
                          DIST_DMGT_model,
                          num_classes,
                          is_isoreg,
                          rare_isoreg,
                          common_isoreg,
                          device,
                          budget):
    
    DIST_DMGT_x = stream_x[0].unsqueeze(0)
    DIST_DMGT_y = stream_y[0].unsqueeze(0)
    
    for i in range(1, len(stream_x)):
        f_values =  class_card(DIST_DMGT_y,
                               stream_x[i],
                               num_classes,
                               DIST_DMGT_model,
                               is_isoreg,
                               rare_isoreg,
                               common_isoreg,
                               device)
        
        if f_values[0] - f_values[1] >= taus[sel_round] and len(DIST_DMGT_x) < budget:
            DIST_DMGT_x = torch.cat((DIST_DMGT_x, stream_x[i].unsqueeze(0)))
            DIST_DMGT_y = torch.cat((DIST_DMGT_y, stream_y[i].unsqueeze(0)))
    
    return DIST_DMGT_x, DIST_DMGT_y

def get_SIEVE_subsets(stream_x,
                      stream_y,
                      SIEVE_model,
                      num_classes,
                      is_isoreg,
                      rare_isoreg,
                      common_isoreg,
                      device,
                      budget,
                      epsilon):

    SIEVE_x = stream_x[0].unsqueeze(0)
    SIEVE_y = stream_y[0].unsqueeze(0)
    
    init_x = SIEVE_x[0]
    init_y = SIEVE_y[0]

    m = 1
    epsilon = 0.1
    j = 1
    O = []
    while (1+epsilon)**j >= m and (1+epsilon)**j <= 2*m*len(stream_x):
        O += [(1+epsilon)**j]
        j += 1
    set_dict = {}
    taus = torch.Tensor().to(device)
    for i in range(1, len(stream_x)):
        new_taus = torch.Tensor().to(device)
        for idx,v in enumerate(O):
            if v not in list(set_dict.keys()):
                set_dict[v] = [(init_x,init_y)]
                taus = torch.cat((taus,torch.tensor([[v]]).to(device)))
            else:
                f_values = class_card(torch.tensor(list(zip(*set_dict[v]))[1]),
                                      stream_x[i],
                                      num_classes,
                                      SIEVE_model,
                                      is_isoreg,
                                      rare_isoreg,
                                      common_isoreg,
                                      device)
                tau = (v/2 - f_values[1])/(budget - len(set_dict[v]))
                new_taus = torch.cat((new_taus,torch.tensor([tau]).to(device)))
                if f_values[0] - f_values[1] >= tau and len(set_dict[v]) < budget:
                    set_dict[v] += [(stream_x[i],stream_y[i])]
        if len(new_taus) > 0:
            taus = torch.cat((taus, new_taus.unsqueeze(1)),dim=1) 

    max_value = -np.inf
    max_key = None
    max_idx = 0
    for idx,key in enumerate(list(set_dict.keys())):
        label_counts = [(torch.tensor(list(zip(*set_dict[key]))[1])==i).float().sum() for i in range(num_classes)]
        if sum([np.sqrt(label_counts[i]) for i in range(num_classes)]) > max_value:
            max_key = key
            max_idx = idx
            max_value = sum([np.sqrt(label_counts[i]) for i in range(num_classes)])
    SIEVE_x = torch.stack(list(zip(*set_dict[max_key]))[0])
    SIEVE_y = torch.stack(list(zip(*set_dict[max_key]))[1])
    sel_taus = taus[max_idx][1:]
    SIEVE_min_max_taus = torch.tensor([sel_taus.min(),sel_taus.max()])
    
    return SIEVE_x, SIEVE_y, SIEVE_min_max_taus

def train(device,
          num_epochs,
          train_loader,
          model):
    
    model = model.to(device)
    
    optimizer = optim.SGD(model.parameters(), lr=0.001, momentum=0.9, weight_decay=0.0005)
    criterion = nn.CrossEntropyLoss()
    
    for epoch in range(num_epochs):
        
        train_loss = 0.0
        train_acc = 0.0
        
        model.train()
        for batch_idx, (data, targets) in enumerate(train_loader):
            
            data, targets = data.to(device), targets.to(device)
            optimizer.zero_grad()
            output = model(data)
            loss = criterion(output, targets.long())
            loss.backward()
            optimizer.step()
            
            train_loss += loss.item()
            train_acc += (output.max(1)[1]==targets).sum().item()

        train_loss = train_loss/len(train_loader.dataset)
        train_acc = train_acc/len(train_loader.dataset)
        
        print('Epoch: {} \tTraining Loss: {:.6f}  Training Acc: {:.6f}'.format(
            epoch, 
            train_loss,
            train_acc,
            ))
   
        if train_acc >= 0.99:
            break
    
    return model

def load_model(model, device):
    
    model_copy = MnistResNet().to(device)
    model_copy.load_state_dict(model.state_dict())
    model_copy = model_copy.to(device)
    return model_copy

def calc_acc(model,
             test_loader,
             num_classes):

    model.eval()
    
    all_acc = []
    rare_acc = []

    for test_idx, (test_x, test_y) in enumerate(test_loader):
        test_x, test_y = test_x.to(device), test_y.to(device)
        
        rare_idxs = (test_y < num_classes/2).nonzero()
        with torch.no_grad():
            
            batch_preds = torch.argmax(model(test_x), dim=1)
            all_acc += [batch_preds.eq(test_y),]
            rare_acc += [batch_preds.eq(test_y)[rare_idxs],]
    
    rare_acc = torch.cat(rare_acc, dim=0)
    all_acc = torch.cat(all_acc, dim=0)
    
    return rare_acc.float().mean().unsqueeze(0), all_acc.float().mean().unsqueeze(0)

def calc_cal_acc(is_isoreg,
                 rare_isoreg,
                 common_isoreg,
                 val_loader,
                 model,
                 num_sm_bins,
                 num_classes,
                 device):

    model.eval()
    score_bins = np.arange(num_sm_bins)/num_classes
    
    rare_scores_dict = {i: [] for i in score_bins}
    common_scores_dict = {i: [] for i in score_bins}
    all_scores_dict = {i: [] for i in score_bins}
    
    for _, (data, targets) in enumerate(val_loader):
        data, targets = data.to(device), targets.to(device)
        with torch.no_grad():
            
            logits = model(data)
            softmax = nn.functional.softmax(logits, dim=1)
            
            top_scores, preds = softmax.max(1)
            
            rare_idxs = (preds < num_classes/2).nonzero()
            common_idxs = (preds >= num_classes/2).nonzero()
            rare_top_scores = top_scores[rare_idxs].cpu().detach().numpy()
            common_top_scores = top_scores[common_idxs].cpu().detach().numpy()
            if is_isoreg:
                rare_top_scores = (np.expand_dims(rare_isoreg.predict(rare_top_scores), axis=1) if 
                        len(rare_idxs)>0 else np.empty(0)) 

                common_top_scores = (np.expand_dims(common_isoreg.predict(common_top_scores), axis=1) if 
                        len(common_idxs)>0 else np.empty(0))

            top_scores = np.append(rare_top_scores, common_top_scores)
            pred_bins = np.floor(10*top_scores)/10
            
            preds = preds[torch.cat((rare_idxs, common_idxs)).squeeze()]
            targets = targets[torch.cat((rare_idxs, common_idxs)).squeeze()]
            
            for i in range(len(targets)):
                key = round(pred_bins[i].item(), 2)
                all_scores_dict[key] += [targets[i]==preds[i]]

                if targets[i] < num_classes/2:
                    rare_scores_dict[key] += [targets[i]==preds[i]]
                
                else:
                    common_scores_dict[key] += [targets[i]==preds[i]]

    cal_rare_acc = torch.tensor(list(map(lambda x: sum(x)/len(x) if len(x)>0 else np.nan, list(rare_scores_dict.values()))))
    cal_common_acc = torch.tensor(list(map(lambda x: sum(x)/len(x) if len(x)>0 else np.nan, list(common_scores_dict.values()))))
    cal_all_acc = torch.tensor(list(map(lambda x: sum(x)/len(x) if len(x)>0 else np.nan, list(all_scores_dict.values()))))
    
    return cal_rare_acc, cal_common_acc, cal_all_acc

def train_isoreg(model, val_loader):
    model.eval()

    top_scores = []
    preds_correct = []
    
    for batch_idx, (data, targets) in enumerate(val_loader):
        data, targets = data.to(device), targets.to(device)
        with torch.no_grad():
            logits = model(data)
            softmax = nn.functional.softmax(logits, dim=1)
            
            top_scores += [softmax.max(1).values,]
            
            preds = softmax.max(1).indices
            preds_correct += [(targets==preds).float(),]
    
    top_scores = torch.cat(top_scores, dim=0)
    preds_correct = torch.cat(preds_correct, dim=0)
    
    IsoReg = IsotonicRegression(y_min=0, y_max=1, increasing=True, out_of_bounds='clip').fit(top_scores.cpu(), preds_correct.cpu())
    return IsoReg 

def get_datasets(num_init_pts,
                 imbal,
                 dataset_name,
                 num_classes):

    data_transform = Compose([Resize((224, 224)), 
                              ToTensor(), 
                              Normalize((0.1307,), (0.3081,))])
    
    mnist_train = MNIST(download=True, 
                        train=True, 
                        root='path to MNIST train data', 
                        transform=data_transform)
    
    rare_idxs = (mnist_train.targets < num_classes/2).nonzero().squeeze(1)
    common_idxs = (mnist_train.targets >= num_classes/2).nonzero().squeeze(1)
    
    common_amount = len(common_idxs)
    rare_amount = int(np.floor(common_amount/imbal))
    
    rare_idxs = rare_idxs[torch.randperm(len(rare_idxs))][:rare_amount]
    common_idxs = common_idxs[torch.randperm(len(common_idxs))][:common_amount]
    
    imbal_idxs = torch.cat((rare_idxs, common_idxs))
    imbal_idxs = imbal_idxs[torch.randperm(len(imbal_idxs))]
    
    init_dataset, stream_dataset = random_split(Subset(mnist_train, imbal_idxs), [num_init_pts, len(imbal_idxs) - num_init_pts])
    
    return init_dataset, stream_dataset

def get_val_loaders(dataset_name,
                    num_test_pts,
                    batch_size,
                    num_workers,
                    num_classes):

    transform = Compose([Resize((224, 224)), 
                         ToTensor(), 
                         Normalize((0.1307,), (0.3081,))])
    
    mnist_test = MNIST(download=True, 
                       train=False, 
                       root='path to MNIST test data', 
                       transform=transform)
    
    rare_idxs = (mnist_test.targets < num_classes/2).nonzero().squeeze(1)
    common_idxs = (mnist_test.targets >= num_classes/2).nonzero().squeeze(1)
    
    test_rare_idxs, val_rare_idxs = rare_idxs[:int(len(rare_idxs)/2)], rare_idxs[int(len(rare_idxs)/2):]
    test_common_idxs, val_common_idxs = common_idxs[:int(len(common_idxs)/2)], common_idxs[int(len(common_idxs)/2):]
    
    test_loader = DataLoader(Subset(mnist_test, torch.cat((test_rare_idxs, test_common_idxs))),
                             batch_size=batch_size,
                             num_workers=num_workers,
                             shuffle=True)

    rare_val_loader = DataLoader(Subset(mnist_test, val_rare_idxs),
                                 batch_size=batch_size,
                                 num_workers=num_workers,
                                 shuffle=True)
    
    common_val_loader = DataLoader(Subset(mnist_test, val_common_idxs),
                                   batch_size=batch_size,
                                   num_workers=num_workers,
                                   shuffle=True)
    
    val_loader = DataLoader(Subset(mnist_test, torch.cat((val_rare_idxs, val_common_idxs))),
                            batch_size=batch_size,
                            num_workers=num_workers,
                            shuffle=True)

    return test_loader, rare_val_loader, common_val_loader, val_loader

def experiment(num_init_pts,
               imbals,
               unif_taus,
               dyn_taus,
               trials,
               num_sel_rounds,
               num_agents,
               num_algs,
               stream_size,
               num_test_pts,
               num_epochs,
               batch_size,
               num_workers,
               num_classes,
               dataset_name,
               num_sm_bins,
               is_isoreg,
               rare_acc_path,
               all_acc_path,
               sizes_path,
               sum_sizes_path,
               model_path,
               device,
               budget,
               epsilon,
               sieve_taus_path):
    
    if not file_exists(rare_acc_path):
        
        rare_acc=torch.zeros(len(trials),num_sel_rounds+1,num_algs)
        all_acc=torch.zeros(len(trials),num_sel_rounds+1,num_algs)
        
        sizes=torch.zeros(len(trials),num_sel_rounds+1,num_algs,num_classes)
        sum_sizes=torch.zeros(len(trials),num_sel_rounds+1,1)
        
        sieve_taus=torch.zeros(len(trials),num_sel_rounds,2)

        test_loader, rare_val_loader, common_val_loader, val_loader = get_val_loaders(dataset_name,
                                                                                      num_test_pts,
                                                                                      batch_size,
                                                                                      num_workers,
                                                                                      num_classes)
        
        init_x = torch.empty(0)
        init_y = torch.empty(0)
        
        stream_datasets_dict = {key: None for key in range(num_agents)}
        
        for agent in range(num_agents):
            
            agent_init_dataset, agent_stream_dataset = get_datasets(num_init_pts,
                                                                    imbals[agent],
                                                                    dataset_name,
                                                                    num_classes)
            
            agent_init_loader = DataLoader(agent_init_dataset,
                                           batch_size=num_init_pts,
                                           num_workers=num_workers,
                                           shuffle=True)

            agent_init_samples = enumerate(agent_init_loader)
            _, (agent_init_x, agent_init_y) = next(agent_init_samples)
            
            init_x = torch.cat((init_x, agent_init_x[:int(np.ceil(num_init_pts/num_agents))]))
            init_y = torch.cat((init_y, agent_init_y[:int(np.ceil(num_init_pts/num_agents))]))
            
            stream_datasets_dict[agent] = agent_stream_dataset
        
        sizes[:,0] = (torch.stack((torch.tensor([(init_y==i).float().sum() for i in range(num_classes)]),
                                   torch.tensor([(init_y==i).float().sum() for i in range(num_classes)]),
                                   torch.tensor([(init_y==i).float().sum() for i in range(num_classes)]),
                                   torch.tensor([(init_y==i).float().sum() for i in range(num_classes)]))))
                
        sum_sizes[:,0] = len(init_x)
                    
        init_loader = DataLoader(TensorDataset(init_x, init_y),
                                 batch_size=batch_size,
                                 num_workers=num_workers,
                                 shuffle=True)

        model = MnistResNet()
        
        if file_exists(model_path):
            model.load_state_dict(torch.load(model_path))
            model = model.to(device)

        else:
            model = train(device,
                          num_epochs,
                          init_loader,
                          model)

            torch.save(model.state_dict(), model_path)
            
        rare_DIST_DMGT_UNIF_isoreg = train_isoreg(model, rare_val_loader) if is_isoreg else None
        common_DIST_DMGT_UNIF_isoreg = train_isoreg(model, common_val_loader) if is_isoreg else None
        rare_DIST_DMGT_DYN_isoreg = train_isoreg(model, rare_val_loader) if is_isoreg else None
        common_DIST_DMGT_DYN_isoreg = train_isoreg(model, common_val_loader) if is_isoreg else None
        rare_SIEVE_isoreg = train_isoreg(model, rare_val_loader) if is_isoreg else None
        common_SIEVE_isoreg = train_isoreg(model, common_val_loader) if is_isoreg else None
        
        rare_acc[:,0] = (

            torch.cat((calc_acc(model, test_loader, num_classes)[0], 
                       calc_acc(model, test_loader, num_classes)[0],
                       calc_acc(model, test_loader, num_classes)[0],
                       calc_acc(model, test_loader, num_classes)[0])))
        
        all_acc[:,0] = (
                
            torch.cat((calc_acc(model, test_loader, num_classes)[1],
                       calc_acc(model, test_loader, num_classes)[1],
                       calc_acc(model, test_loader, num_classes)[1],
                       calc_acc(model, test_loader, num_classes)[1])))
        
        for trial in trials: 
            
            DIST_DMGT_UNIF_model = load_model(model, device)
            DIST_DMGT_DYN_model = load_model(model, device)
            RAND_model = load_model(model, device)
            SIEVE_model = load_model(model, device)
            
            stream_loaders_dict = {agent: DataLoader(stream_datasets_dict[agent],
                                                     batch_size=stream_size,
                                                     num_workers=num_workers,
                                                     shuffle=True) for agent in range(num_agents)}

            stream_samples_dict = {agent: enumerate(stream_loaders_dict[agent]) for agent in range(num_agents)}
            
            for sel_round in range(num_sel_rounds):
                
                DIST_DMGT_UNIF_x = torch.empty(0)
                DIST_DMGT_UNIF_y = torch.empty(0)
                DIST_DMGT_DYN_x = torch.empty(0)
                DIST_DMGT_DYN_y = torch.empty(0)
                SIEVE_x = torch.empty(0)
                SIEVE_y = torch.empty(0)
                
                stream_x = torch.empty(0)
                stream_y = torch.empty(0)

                for agent in range(num_agents):
                    
                    _, (agent_stream_x, agent_stream_y) = next(stream_samples_dict[agent])

                    agent_DIST_DMGT_UNIF_x, agent_DIST_DMGT_UNIF_y = get_DIST_DMGT_subsets(agent_stream_x,
                                                                                 agent_stream_y,
                                                                                 unif_taus,
                                                                                 sel_round,
                                                                                 DIST_DMGT_UNIF_model,
                                                                                 num_classes,
                                                                                 is_isoreg,
                                                                                 rare_DIST_DMGT_UNIF_isoreg,
                                                                                 common_DIST_DMGT_UNIF_isoreg,
                                                                                 device,
                                                                                 budget)
                        
                    agent_DIST_DMGT_DYN_x, agent_DIST_DMGT_DYN_y = get_DIST_DMGT_subsets(agent_stream_x,
                                                                                 agent_stream_y,
                                                                                 dyn_taus,
                                                                                 sel_round,
                                                                                 DIST_DMGT_DYN_model,
                                                                                 num_classes,
                                                                                 is_isoreg,
                                                                                 rare_DIST_DMGT_DYN_isoreg,
                                                                                 common_DIST_DMGT_DYN_isoreg,
                                                                                 device,
                                                                                 budget)
                    
                    agent_SIEVE_x, agent_SIEVE_y, _ = get_SIEVE_subsets(agent_stream_x,
                                                                        agent_stream_y,
                                                                        SIEVE_model,
                                                                        num_classes,
                                                                        is_isoreg,
                                                                        rare_SIEVE_isoreg,
                                                                        common_SIEVE_isoreg,
                                                                        device,
                                                                        budget,
                                                                        epsilon)

                    DIST_DMGT_UNIF_x = torch.cat((DIST_DMGT_UNIF_x, agent_DIST_DMGT_UNIF_x))                
                    DIST_DMGT_UNIF_y = torch.cat((DIST_DMGT_UNIF_y, agent_DIST_DMGT_UNIF_y))
                    DIST_DMGT_DYN_x = torch.cat((DIST_DMGT_DYN_x, agent_DIST_DMGT_DYN_x))                
                    DIST_DMGT_DYN_y = torch.cat((DIST_DMGT_DYN_y, agent_DIST_DMGT_DYN_y))
                    SIEVE_x = torch.cat((SIEVE_x, agent_SIEVE_x))                
                    SIEVE_y = torch.cat((SIEVE_y, agent_SIEVE_y))
                    stream_x = torch.cat((stream_x, agent_stream_x))
                    stream_y = torch.cat((stream_y, agent_stream_y))

                cent_DIST_DMGT_UNIF_x, cent_DIST_DMGT_UNIF_y = get_DIST_DMGT_subsets(DIST_DMGT_UNIF_x,
                                                                           DIST_DMGT_UNIF_y,
                                                                           unif_taus,
                                                                           sel_round,
                                                                           DIST_DMGT_UNIF_model,
                                                                           num_classes,
                                                                           is_isoreg,
                                                                           rare_DIST_DMGT_UNIF_isoreg,
                                                                           common_DIST_DMGT_UNIF_isoreg,
                                                                           device,
                                                                           budget)
                
                cent_DIST_DMGT_DYN_x, cent_DIST_DMGT_DYN_y = get_DIST_DMGT_subsets(DIST_DMGT_DYN_x,
                                                                           DIST_DMGT_DYN_y,
                                                                           dyn_taus,
                                                                           sel_round,
                                                                           DIST_DMGT_DYN_model,
                                                                           num_classes,
                                                                           is_isoreg,
                                                                           rare_DIST_DMGT_DYN_isoreg,
                                                                           common_DIST_DMGT_DYN_isoreg,
                                                                           device,
                                                                           budget)

                cent_SIEVE_x, cent_SIEVE_y, cent_min_max_taus = get_SIEVE_subsets(SIEVE_x,
                                                                                  SIEVE_y,
                                                                                  SIEVE_model,
                                                                                  num_classes,
                                                                                  is_isoreg,
                                                                                  rare_SIEVE_isoreg,
                                                                                  common_SIEVE_isoreg,
                                                                                  device,
                                                                                  budget,
                                                                                  epsilon)

                rand_idxs = torch.randperm(len(stream_x))[:budget]
                cent_RAND_x = stream_x[rand_idxs]
                cent_RAND_y = stream_y[rand_idxs]

                sieve_taus[trial,sel_round] = cent_min_max_taus
                
                sizes[trial,sel_round+1] = (       
                      sizes[trial,sel_round] + 
                      torch.stack((torch.tensor([(cent_DIST_DMGT_UNIF_y==i).float().sum() for i in range(num_classes)]),
                                   torch.tensor([(cent_DIST_DMGT_DYN_y==i).float().sum() for i in range(num_classes)]),
                                   torch.tensor([(cent_RAND_y==i).float().sum() for i in range(num_classes)]),
                                   torch.tensor([(cent_RAND_y==i).float().sum() for i in range(num_classes)]))))
                      
                sum_sizes[trial,sel_round+1] = (
                          torch.tensor([sum_sizes[trial,sel_round] + len(cent_DIST_DMGT_UNIF_y)]))

                DIST_DMGT_UNIF_model = train(device,
                                        num_epochs,
                                        DataLoader(TensorDataset(cent_DIST_DMGT_UNIF_x, cent_DIST_DMGT_UNIF_y),
                                                   batch_size=batch_size,
                                                   num_workers=num_workers,
                                                   shuffle=True),
                                        DIST_DMGT_UNIF_model)
                
                DIST_DMGT_DYN_model = train(device,
                                        num_epochs,
                                        DataLoader(TensorDataset(cent_DIST_DMGT_DYN_x, cent_DIST_DMGT_DYN_y),
                                                   batch_size=batch_size,
                                                   num_workers=num_workers,
                                                   shuffle=True),
                                        DIST_DMGT_DYN_model)
                
                RAND_model = train(device,
                                   num_epochs,
                                   DataLoader(TensorDataset(cent_RAND_x, cent_RAND_y),
                                              batch_size=batch_size,
                                              num_workers=num_workers,
                                              shuffle=True),
                                   RAND_model)
                            
                SIEVE_model = train(device,
                                    num_epochs,
                                    DataLoader(TensorDataset(cent_SIEVE_x, cent_SIEVE_y),
                                               batch_size=batch_size,
                                               num_workers=num_workers,
                                               shuffle=True),
                                    SIEVE_model)

                rare_DIST_DMGT_UNIF_isoreg = train_isoreg(DIST_DMGT_UNIF_model, rare_val_loader) if is_isoreg else None
                common_DIST_DMGT_UNIF_isoreg = train_isoreg(DIST_DMGT_UNIF_model, common_val_loader) if is_isoreg else None
                rare_DIST_DMGT_DYN_isoreg = train_isoreg(DIST_DMGT_DYN_model, rare_val_loader) if is_isoreg else None
                common_DIST_DMGT_DYN_isoreg = train_isoreg(DIST_DMGT_DYN_model, common_val_loader) if is_isoreg else None
                rare_SIEVE_isoreg = train_isoreg(SIEVE_model, rare_val_loader) if is_isoreg else None
                common_SIEVE_isoreg = train_isoreg(SIEVE_model, common_val_loader) if is_isoreg else None
        
                rare_acc[trial,sel_round+1] = (
                         torch.cat((calc_acc(DIST_DMGT_UNIF_model, test_loader, num_classes)[0], 
                                    calc_acc(DIST_DMGT_DYN_model, test_loader, num_classes)[0],
                                    calc_acc(RAND_model, test_loader, num_classes)[0],
                                    calc_acc(SIEVE_model, test_loader, num_classes)[0])))
                
                all_acc[trial,sel_round+1] = (
                        torch.cat((calc_acc(DIST_DMGT_UNIF_model, test_loader, num_classes)[1],
                                   calc_acc(DIST_DMGT_DYN_model, test_loader, num_classes)[1],
                                   calc_acc(RAND_model, test_loader, num_classes)[1],
                                   calc_acc(SIEVE_model, test_loader, num_classes)[1])))

        torch.save(rare_acc, rare_acc_path)
        torch.save(all_acc, all_acc_path)

        torch.save(sizes, sizes_path)
        torch.save(sum_sizes, sum_sizes_path)
        
        torch.save(sieve_taus, sieve_taus_path)

    rare_acc = torch.load(rare_acc_path)
    all_acc = torch.load(all_acc_path)

    sizes = torch.load(sizes_path)
    sum_sizes = torch.load(sum_sizes_path)
    
    sieve_taus = torch.load(sieve_taus_path)

    return rare_acc, all_acc, sizes, sum_sizes, sieve_taus

def dataframe(data,
              trials,
              num_sel_rounds):

    rare_acc, all_acc, sizes, sum_sizes, sieve_taus = data
    
    df = pd.DataFrame(columns=['trial',
                               'sel_rnd',
                               'DIST_DMGT_UNIF_all_acc',
                               'DIST_DMGT_DYN_all_acc',
                               'RAND_all_acc',
                               'SIEVE_all_acc',
                               'DIST_DMGT_UNIF_rare_acc',
                               'DIST_DMGT_DYN_rare_acc',
                               'RAND_rare_acc',
                               'SIEVE_rare_acc',
                               'DIST_DMGT_UNIF_rare_amnt',
                               'DIST_DMGT_DYN_rare_amnt',
                               'RAND_rare_amnt',
                               'SIEVE_rare_amnt',
                               'DIST_DMGT_UNIF_common_amnt',
                               'DIST_DMGT_DYN_common_amnt',
                               'RAND_common_amnt',
                               'SIEVE_common_amnt',
                               'sum_sizes',
                               'sum_sizes_perc'])
    
    for trial in trials:
        df = df.append(pd.DataFrame({'trial':trial*torch.ones(num_sel_rounds+1),
                                     'sel_rnd':torch.arange(num_sel_rounds+1),
                                     'DIST_DMGT_UNIF_all_acc':all_acc[trial,:,0].squeeze(),
                                     'DIST_DMGT_DYN_all_acc':all_acc[trial,:,1].squeeze(),
                                     'RAND_all_acc':all_acc[trial,:,2].squeeze(),
                                     'SIEVE_all_acc':all_acc[trial,:,3].squeeze(),
                                     'DIST_DMGT_UNIF_rare_acc':rare_acc[trial,:,0].squeeze(),
                                     'DIST_DMGT_DYN_rare_acc':rare_acc[trial,:,1].squeeze(),
                                     'RAND_rare_acc':rare_acc[trial,:,2].squeeze(),
                                     'SIEVE_rare_acc':rare_acc[trial,:,3].squeeze(),
                                     'DIST_DMGT_UNIF_rare_amnt':(torch.stack([x[:5].sum().int() for x in sizes[trial,:,0]])),
                                     'DIST_DMGT_DYN_rare_amnt':(torch.stack([x[:5].sum().int() for x in sizes[trial,:,1]])),
                                     'RAND_rare_amnt':(torch.stack([x[:5].sum().int() for x in sizes[trial,:,2]])),
                                     'SIEVE_rare_amnt':(torch.stack([x[:5].sum().int() for x in sizes[trial,:,3]])),
                                     'DIST_DMGT_UNIF_common_amnt':(torch.stack([x[5:].sum().int() for x in sizes[trial,:,0]])),
                                     'DIST_DMGT_DYN_common_amnt':(torch.stack([x[5:].sum().int() for x in sizes[trial,:,1]])),
                                     'RAND_common_amnt':(torch.stack([x[5:].sum().int() for x in sizes[trial,:,2]])),
                                     'SIEVE_common_amnt':(torch.stack([x[5:].sum().int() for x in sizes[trial,:,3]])),
                                     'sum_sizes':sum_sizes[trial].squeeze().int(),
                                     'sum_sizes_perc':(sum_sizes[trial].squeeze().int()/(30*(num_sel_rounds+1)))}),
                                     ignore_index=True)
    
    return df

def accuracy_plot(df, date_time, num_algs, num_sel_rounds):
    
    fig, ax = plt.subplots()
    sns.despine()
    
    all_acc_data_files = ['DIST_DMGT_UNIF_all_acc','DIST_DMGT_DYN_all_acc','RAND_all_acc','SIEVE_all_acc'] 
    rare_acc_data_files = ['DIST_DMGT_UNIF_rare_acc','DIST_DMGT_DYN_rare_acc','RAND_rare_acc','SIEVE_rare_acc']
    
    all_acc_labels = ['Filt. Dist. DMGT w/ Uniform Thresholds: all classes',
                      'Filt. Dist. DMGT w/ Increasing Thresholds: all classes',
                      'RAND: all classes',
                      'SIEVE: all classes']
    
    rare_acc_labels = ['Filt. Dist. DMGT w/ Uniform Thresholds: rare classes',
                       'Filt. Dist. DMGT w/ Increasing Thresholds: rare classes',
                       'RAND: rare classes',
                       'SIEVE: rare classes']

    for i in range(num_algs):
        sns.lineplot(data=df[['sel_rnd', all_acc_data_files[i]]],
                     x='sel_rnd',
                     y=all_acc_data_files[i],
                     color=sns.color_palette('muted')[i],
                     ci=95,
                     estimator='mean',
                     label=all_acc_labels[i],
                     marker='*',
                     markerfacecolor='black',
                     linestyle='--',
                     markersize=8)

        sns.lineplot(data=df[['sel_rnd', rare_acc_data_files[i]]],
                     x='sel_rnd',
                     y=rare_acc_data_files[i],
                     color=sns.color_palette('pastel')[i],
                     ci=95,
                     estimator='mean',
                     label=rare_acc_labels[i],
                     marker='*',
                     markerfacecolor='black',
                     markersize=8)
    
    ax.legend(fontsize=6)
    ax.set_xlabel('Selection Round')
    ax.set_ylabel('Accuracy')
    fig.tight_layout()
    fig.savefig(img_dir + date_time + '_accuracy.pdf')

def balance_plot(date_time,sizes,num_algs,num_classes,num_sel_rounds,unif_taus,dyn_taus):
    alg_names = ['Filt. Dist. DMGT w/ Uniform Thresholds','Filt. Dist. DMGT w/ Increasing Thresholds','RAND','SIEVE']
    avg_sizes = sizes.mean(dim=0)
    non_cum_sizes = torch.zeros(num_algs, num_classes, num_sel_rounds)
    for i in range(num_algs):
        for j in range(num_classes):
            per_class_sizes = list(avg_sizes.transpose(0,1).transpose(1,2)[i,j])
            non_cum_sizes[i,j] = torch.tensor([per_class_sizes[i+1]-per_class_sizes[i] for i in range(num_sel_rounds)])
    
    non_cum_sizes = non_cum_sizes.transpose(1,2).transpose(0,1)

    rare_avg_sizes = non_cum_sizes[:,:,:5].mean(dim=2)
    common_avg_sizes = non_cum_sizes[:,:,5:].mean(dim=2)

    rare_common_avg_sizes = torch.cat((rare_avg_sizes.unsqueeze(2), common_avg_sizes.unsqueeze(2)),dim=2)
    fig, ax = plt.subplots()
    for i in range(num_algs):
        sns.lineplot(x=np.arange(num_sel_rounds),
                     y=rare_common_avg_sizes[:,i,0],
                     color=sns.color_palette('pastel')[i],
                     label=alg_names[i] + ' rare classes',
                     marker='*',
                     markerfacecolor='black',
                     markersize=8)
        sns.lineplot(x=np.arange(num_sel_rounds),
                     y=rare_common_avg_sizes[:,i,1],
                     color=sns.color_palette('muted')[i],
                     label=alg_names[i] + ' common classes',
                     linestyle='--',
                     marker='*',
                     markerfacecolor='black',
                     markersize=8)
    
    n = Symbol('n')
    unif_balanced_size = int(np.ceil(solve(sympy.sqrt(n+1) - sympy.sqrt(n) - unif_taus[0], n)[0]))
    sns.lineplot(x=np.arange(num_sel_rounds),
                 y=unif_balanced_size,
                 color='gray',
                 label=r'Balanced Uniform $\tau$', 
                 linestyle='--')
    
    n = Symbol('n')
    dyn_balanced_sizes = [int(np.ceil(solve(sympy.sqrt(n+1) - sympy.sqrt(n) - dyn_taus[i], n)[0])) for i in range(len(dyn_taus))]
    sns.lineplot(x=np.arange(num_sel_rounds),
                 y=dyn_balanced_sizes,
                 color='black',
                 label=r'Balanced Increasing $\tau$',
                 linestyle='--')
    
    ax.set_xlabel('Selection Round')
    ax.set_ylabel('Average Number of Points per Class')
    ax.legend(fontsize=5,ncol=2,loc='upper right')
    fig.tight_layout()
    sns.despine()
    fig.savefig(img_dir + date_time + '_balance.pdf')
    
parser = argparse.ArgumentParser()
parser.add_argument('--init_pts', nargs='+', type=int, default=[1000])
parser.add_argument('--imbals', nargs='+', type=int, default=[2,5,10])
parser.add_argument('--unif_taus', nargs='+', type=float, default=8*[0.1])
parser.add_argument('--dyn_taus', nargs='+', type=float, default=[0.1,0.1,0.13,0.13,0.15,0.15,0.17,0.2])
parser.add_argument('--num_agents', type=int, default=3)
parser.add_argument('--trials', nargs='+', type=int, default=np.arange(3))
parser.add_argument('--num_sel_rounds', type=int, default=8)
parser.add_argument('--num_algs', type=int, default=4)
parser.add_argument('--stream_size', type=int, default=500)
parser.add_argument('--num_test_pts', type=int, default=5000)
parser.add_argument('--num_epochs', type=int, default=200)
parser.add_argument('--batch_size', type=int, default=256)
parser.add_argument('--num_workers', type=int, default=10)
parser.add_argument('--num_classes', type=int, default=10)
parser.add_argument('--dataset_name', type=str, default='MNIST')
parser.add_argument('--num_sm_bins', type=int, default=11) 
parser.add_argument('--is_isoreg', type=bool, default=True)
parser.add_argument('--seed', type=int, default=0)
parser.add_argument('--budget', type=int, default=250)
parser.add_argument('--epsilon', type=float, default=0.1)

if __name__ == "__main__":
    
    args = parser.parse_args()
    device=torch.device('cuda:0') 

    # fix randomness
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    random.seed(args.seed) 
    torch.backends.cudnn.deterministic=True
    
    img_dir = 'plots directory'
    #os.mkdir(img_dir)

    val_dir = 'cached data files directory'
    #os.mkdir(val_dir)

    model_path = 'path to initially trained model'
    
    # accuracy paths
    all_acc_path=val_dir + 'all_acc.pkl'
    rare_acc_path=val_dir + 'rare_acc.pkl'

    # labeled set sizes paths
    sizes_path=val_dir + 'sizes.pkl'
    sum_sizes_path=val_dir + 'sum_sizes.pkl'
    
    # min and max thresholds from sieve algorithm
    sieve_taus_path=val_dir + 'sieve_taus.pkl'

    # class mat path
    input_args = [args.init_pts[0],
                  args.imbals,
                  args.unif_taus,
                  args.dyn_taus,
                  args.trials,
                  args.num_sel_rounds,
                  args.num_agents,
                  args.num_algs,
                  args.stream_size,
                  args.num_test_pts,
                  args.num_epochs,
                  args.batch_size,
                  args.num_workers,
                  args.num_classes,
                  args.dataset_name,
                  args.num_sm_bins,
                  args.is_isoreg,
                  rare_acc_path,
                  all_acc_path,
                  sizes_path,
                  sum_sizes_path,
                  model_path,
                  device,
                  args.budget,
                  args.epsilon,
                  sieve_taus_path]

    rare_acc, all_acc, sizes, sum_sizes, sieve_taus = experiment(*input_args)
    
    df = dataframe(experiment(*input_args),
                   args.trials,
                   args.num_sel_rounds)

    balance_plot(date_time,sizes,args.num_algs,args.num_classes,args.num_sel_rounds,args.unif_taus,args.dyn_taus)
    accuracy_plot(df,date_time,args.num_algs,args.num_sel_rounds)
