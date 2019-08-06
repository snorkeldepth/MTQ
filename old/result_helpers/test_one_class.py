from os.path import join
from typing import Tuple
import torch.optim as optim
import numpy as np
import torch
from prettytable import PrettyTable
from torch.utils.data import DataLoader

from tqdm import tqdm
from datasets.utils import set_random_seed

from datasets.base import OneClassDataset
from models.base import BaseModule
from models.loss_functions import * 
from datasets.utils import novelty_score
from datasets.utils import normalize

from result_helpers.utils import *

from sklearn.metrics import roc_auc_score
from sklearn.metrics import precision_recall_fscore_support

from models import *
import math

# for cdf 
from scipy.stats import norm

# from result_helpers import metric_method as mm



class OneClassTestHelper(object):
    """
    Performs tests for one-class datasets (MNIST or CIFAR-10).
    """

    def __init__(self, dataset, model, score_normed, novel_ratio, lam, checkpoints_dir, output_file, device,batch_size, trainflag, lr, epochs, before_log_epochs, combined, pretrained, add, noise,fixed= False, pretrained_model = 'LSA_SOS', mulobj=False, quantile_flag = False, checkpoint = None):
        # type: (OneClassDataset, BaseModule, str, str) -> None
        """
        Class constructor.

        :param dataset: dataset class.
        :param model: py-torch model to evaluate.
        :param score_normed: 1 normalized the novelty score with valid set, 0: not normalized
        :param novel_ratio: novel_ratio in test sets
        :param checkpoints_dir: directory holding checkpoints for the model.
        :param output_file: text file where to save results.
        """
        self.dataset = dataset
        self.model = model
        # save initialization
        torch.save(self.model.state_dict(), join(checkpoints_dir, f'{model.name}_start.pkl'))
        self.combined = combined
        self.checkpoints_dir = checkpoints_dir
        print(checkpoints_dir)
        self. checkpoint = checkpoint
        self.output_file = output_file
        self.device = device
        self.name = model.name
        self.batch_size = batch_size
        self.lam = lam
        self.noise = noise 


        # control novel ratio in test sets.
        self.novel_ratio = novel_ratio

        self.trainflag = trainflag # whether need train
        self.quantile_flag = quantile_flag # whether use quantile
        # self.add = add # whether directly use two pretrained model
        self.mulobj= mulobj# whether use mul-gradient
        self.score_normed = score_normed # normalized novelty score
        

        # Set up loss function
        # encoder + decoder
        if self.name in ['LSA','AAE']:
            self.loss =LSALoss(cpd_channels=100)
        # encoder + estimator+ decoder
        elif self.name == 'LSA_EN':
            self.loss = LSAENLoss(cpd_channels=100,lam=lam)
        elif self.name == 'LSA_SOS':
            self.loss =LSASOSLoss(lam)
        elif self.name == 'AAE_SOS':
            self.loss =LSASOSLoss(lam)
        elif self.name == 'LSA_MAF':
            self.loss =LSAMAFLoss(lam)
        elif self.name == 'LSA_QT':
            self.loss = LSAQTLoss(lam)
        
        # encoder + estimator
        elif self.name == 'LSA_ET_EN':
            self.loss = LSAETENLoss(cpd_channels=100)
        elif self.name == 'LSA_ET_SOS':
            self.loss = LSAETSOSLoss()
        elif self.name == 'LSA_ET_MAF':
            self.loss =LSAETMAFLoss()
        elif self.name == 'LSA_ET_QT':
            self.loss =LSAETQTLoss() 
        # only estimator
        elif self.name == 'SOS':
            self.loss =SOSLoss()
        else:
            ValueError("Wrong Model Name")
        
        print (f"Testing on {self.name}")

        # initialize dir
        self.model_dir = None
        self.best_model_dir = None
        self.result_dir = None

        # if self.pretrained:
        #     self.ae_finished = True
        # else:
        self.ae_finished = False

        self.fixed =fixed
        self.pretrained = pretrained
        # Related to training
        if trainflag:
            self.optimizer = optim.Adam(self.model.parameters(), lr= lr, weight_decay=1e-6)
            self.est_optimizer = optim.Adam(self.model.estimator.parameters(),lr = lr, weight_decay = 1e-6)
            self.ae_optimizer  = optim.Adam(list(self.model.encoder.parameters())+list(self.model.decoder.parameters()),lr = 0.0001, weight_decay = 1e-6)

            
            # self.from_pretrained = from_pretrained
            self.pretrained_model = pretrained_model
            self.lr = lr
            self.train_epoch = epochs
            self.before_log_epochs = before_log_epochs
        
        
    def get_path(self):
        name    = self.name
        cl      = self.cl 
        checkpoints_dir = self.checkpoints_dir
        checkpoint = self.checkpoint 
        lam     = self.lam 
        
        if self.mulobj:
            self.model_dir = join(checkpoints_dir,f'{cl}{name}_mul.pkl')
            self.best_model_dir = join(checkpoints_dir,f'{cl}{name}_mul_b.pkl')
            self.best_model_rec_dir = join(checkpoints_dir,f'{cl}{name}_mul_rec_b.pkl')
            self.result_dir = join(checkpoints_dir,f'{cl}{name}_mul_history.npy')
        else:
            self.model_dir = join(checkpoints_dir,f'{cl}{name}_{lam}.pkl')
            self.best_model_dir = join(checkpoints_dir,f'{cl}{name}_{lam}_b.pkl')
            self.best_model_rec_dir = join(checkpoints_dir,f'{cl}{name}_{lam}_rec_b.pkl')
            self.result_dir = join(checkpoints_dir,f'{cl}{name}_{lam}_history.npy')
    
        if (not (self.checkpoint ==None)) and (not self.trainflag):
                # select one epoch to test
                if self.mulobj:
                    self.model_dir = join(checkpoints_dir,f'{cl}{self.name}_mul_{checkpoint}.pkl')
                else:
                    self.model_dir = join(checkpoints_dir,f'{cl}{self.name}_{lam}_{checkpoint}.pkl')
        
    def _eval_quantile(self, s, method_name='n_cdf'):
    #  from s~N(R^d) to u~(0,1)^d
    #  u_i = phi(s_i), where phi is the cdf of N(0,1)
        bs = s.shape[0]
        s_dim = s.shape[1]
        s_numpy = s.cpu().numpy()
        q1 = []
        q2 = []
        qinf = []
        if method_name=='n_cdf':
            for i in range(bs):
                # for every sample
                # cdf 
                u = norm.cdf(s_numpy[i,:])
                u = abs(np.ones((1,s_dim))*0.5-u)
                # norm 1 
                uq_1 = np.linalg.norm(u,1)
                # norm 2 
                uq_2 = np.linalg.norm(u)
                # norm inf 
                uq_inf = np.linalg.norm(u,np.inf)

                q1.append(-uq_1)

                q2.append(-uq_2)

                qinf.append(-uq_inf)
        else:
            ValueError("Unknown Mapping")

        return q1, q2, qinf
    
    def _eval(self, x, average = True, quantile_flag = False):

        if self.name in ['LSA','AAE']:
            # ok
            x_r = self.model(x)
            tot_loss = self.loss(x, x_r,average)

        elif self.name == 'LSA_EN':
            x_r, z, z_dist = self.model(x)
            tot_loss = self.loss(x, x_r, z, z_dist,average)


        elif self.name in ['LSA_SOS', 'LSA_MAF','AAE_SOS']:
            x_r, z, s, log_jacob_T_inverse = self.model(x)
            tot_loss = self.loss(x, x_r, s, log_jacob_T_inverse,average)
            # compute quantiles
            if quantile_flag:
                q1,q2,qinf = self._eval_quantile(s)
                return tot_loss, q1, q2,qinf

        elif self.name in ['SOS', 'MAF','LSA_ET_MAF','LSA_ET_SOS']:
            s, log_jacob_T_inverse = self.model(x)
            tot_loss = self.loss(s, log_jacob_T_inverse,average)
            if quantile_flag:
                q1,q2,qinf = self._eval_quantile(s)
                return tot_loss, q1,q2,qinf

        elif self.name in ['LSA_ET_EN']:
            z, z_dist = self.model(x)
            tot_loss = self.loss(z, z_dist) 

        return tot_loss

    # def load_pretrained_model(self, model_name,cl):
    #     print(f"load pretraind")
    #     if self.pretrained:
    #         if model_name == 'LSA':
                
    #             self.model.load_state_dict(torch.load(f'checkpoints/{self.dataset.name}/combined{self.combined}/PtrFalse/{cl}LSA_{self.lam}.pkl'),strict = False)
            
    #         elif model_name in ['LSA_SOS']:
    #             print('LSA_SOS')
    #             self.model.load_state_dict(torch.load(f'checkpoints/{self.dataset.name}/combined{self.combined}/PtrFalse/FixTrue/b1h2048/{cl}{model_name}_1_rec_b.pkl.pkl'))
    #         else:
    #             ValueError("Setting For New Pretrained Model")





    # def add_two_pretrained_model(self, cl):
    #     ####load model1
    #     dataset_name = self.dataset.name
    #     lam = self.lam


    #     model_dict = self.model.state_dict()
    #     pretrained_model1 = LSA_MNIST(input_shape=self.dataset.shape, code_length=64, combine_density = False).cuda()
    #     pretrained_model1.load_w(f'checkpoints/{self.dataset.name}/combined{self.combined}/PtrFalse/{cl}LSA.pkl')
    #     print("Load the Pretrained LSA")

    #     pretrained_dict1= pretrained_model1.state_dict()
    #     pretrained_dict1 = {k: v for k, v in pretrained_dict1.items() if k in model_dict} 

    #     model_dict.update(pretrained_dict1) 
    #     self.model.load_state_dict(model_dict)

    #     ######load model2
    #     model_dict = self.model.state_dict()
    #     pretrained_model2 = LSAET_MNIST(input_shape=self.dataset.shape, code_length=64, num_blocks=1, est_name = 'SOS',hidden_size= 2048).cuda()
    #     print("Load the Pretrained LSA_ET")
        
    #     pretrained_model2.load_w(f'checkpoints/{self.dataset.name}/combined{self.combined}/PtrTrue/FixTrue/{cl}LSA_ET_SOS_{lam}.pkl') ## select best or last?

    #     pretrained_dict2= pretrained_model2.state_dict()
    #     pretrained_dict2 = {k: v for k, v in pretrained_dict2.items() if k in model_dict}

    #     # update & load
    #     model_dict.update(pretrained_dict2) 
    #     self.model.load_state_dict(model_dict)


    def train_every_epoch(self, epoch, cl):
      
            epoch_loss = 0
            epoch_recloss = 0
            epoch_nllk = 0

            # if self.dataset.name == 'mnist' and self.name in['LSA_SOS','LSA_MAF']:
            #     if epoch < 100:
            #         newlr =0.0001
            #     elif epoch <5000:
            #         newlr = 0.00001
            #     else:
            #         newlr  = 0.000001
            #     for param_group in self.optimizer.param_groups:
            #                 param_group['lr'] = newlr

            noise = self.noise 
            if noise>0:
                self.dataset.train(normal_class =cl, noise_ratio= noise)
            else:
                self.dataset.train(cl)
            loader = DataLoader(self.dataset, batch_size = self.batch_size,shuffle=False)
            dataset_name = self.dataset.name
            epoch_size = self.dataset.length
            pbar = tqdm(total=epoch_size)
            s_alpha = 0

            for batch_idx, (x , y) in enumerate(loader):
                
                x = x.to(self.device)
                # self.model.zero_grad()
                self.optimizer.zero_grad()
                
                # backward average loss along batch
                if (self.mulobj):
                    if (self.ae_finished or (self.fixed ==False)):
                    # Multi-objective Optimization
                        # g1: the gradient of reconstruction loss w.r.t the parameters of encoder
                        # g2: the gradient of auto-regression loss w.r.t the parameters of encoder
                        self._eval(x)
                        # Backward Total loss= Reconstruction loss + Auto-regression Loss
                        torch.autograd.backward(self.loss.autoregression_loss+self.loss.reconstruction_loss, self.model.parameters(),retain_graph =True)
                        #g1_list = g1 + g2
                        g1_list= [pi.grad.data.clone() for pi in list(self.model.encoder.parameters())]    
                        # Backward Auto-regression Loss, the gradients computed in the first backward are not cleared
                        torch.autograd.backward(self.loss.autoregression_loss,list(self.model.encoder.parameters())+list(self.model.estimator.parameters()))
                        # torch.autograd.backward(self.loss.autoregression_loss, self.model.parameters())
                        
                        #g2_list = g1_list + g2
                        g2_list= [pi.grad.data.clone() for pi in list(self.model.encoder.parameters())]
                        
                        # the gradients w.r.t estimator are accumulated, div 2 to get the original one
                        for p in self.model.estimator.parameters():
                            p.grad.data.div(2.0)

                        # compute alpha
                        top = 0
                        down = 0
                    

                        # the formula (4) in Multi_Task Learning as Multi-Objective Function
                        i =0
                        for p in self.model.encoder.parameters():
                            g2   =  (g2_list[i]-g1_list[i])
                            g1   =  (g1_list[i]-g2)

                            g1 = g1.view(-1,)
                            g2 = g2.view(-1,)


                            top   =  top + torch.dot((g2-g1),g2).sum()

                            down  =  down+ torch.pow((g1-g2),2).sum()
                            i     =  i + 1

                        if down ==0:
                            alpha = 0.5
                        else:
                            # print(top)
                            alpha = (top/down).item()
                            alpha = max(min(alpha,1),0)
                            
                        # compute new gradient of Shared Encoder by combined two gradients
                        i=0
            
                        s_alpha =s_alpha + alpha*x.shape[0]

                        for p in self.model.encoder.parameters():
                            newlrg2 = g2_list[i]-g1_list[i]
                            newlrg1 = 2*g1_list[i]-g2_list[i]
                            # compute the multi-gradient of the parameters in the encoder
                            # p.grad.zero_()
                            p.grad.data = torch.mul(newlrg1,alpha)+torch.mul(newlrg1, 1-alpha)
                            i = i+1

                        self.optimizer.step()

                    else:
                        self._eval(x)
                        self.loss.reconstruction_loss.backward()
                        self.ae_optimizer.step()
                
                elif self.fixed:
                    self._eval(x)
                    
                    if (not self.ae_finished):
                        self.loss.reconstruction_loss.backward()
                        self.ae_optimizer.step()
                    else:
                        self.loss.autoregression_loss.backward()
                        self.est_optimizer.step()
                
                else:    
                    self._eval(x).backward()
                    self.optimizer.step()




                
                epoch_loss += self.loss.total_loss.item()*x.shape[0]

                if self.name in ['LSA_EN','LSA_SOS','LSA_MAF','AAE_SOS']:
                    epoch_recloss +=self.loss.reconstruction_loss.item()*x.shape[0]
                    epoch_nllk += self.loss.autoregression_loss.item()*x.shape[0]

                pbar.update(x.size(0))
                pbar.set_description('Train, Loss: {:.6f}'.format(epoch_loss / (pbar.n)))


            pbar.close()

            # print epoch result
            if self.name in ['LSA_EN','LSA_SOS','LSA_MAF','AAE_SOS']:
                
                print('{}Train Epoch-{}: {}\tLoss: {:.6f}\tRec: {:.6f}\tNllk: {:.6f}'.format(self.name,
                        self.dataset.normal_class, epoch, epoch_loss/epoch_size, epoch_recloss/epoch_size, epoch_nllk/epoch_size))
            else:
                print('Train Epoch-{}: {}\tLoss:{:.6f}\t'.format(
                        self.dataset.normal_class, epoch, epoch_loss/epoch_size))
            
            if self.mulobj:
                print (f'Adaptive Alpha:{s_alpha/epoch_size}')

            return epoch_loss/epoch_size, epoch_recloss/epoch_size, epoch_nllk/epoch_size
    

    def validate(self, epoch, cl):

        prefix = 'Validation'
        self.model.eval()

        val_loss = 0
        val_nllk=0
        val_rec =0

        self.dataset.val(cl)
        loader = DataLoader(self.dataset, self.batch_size)

        epoch_size = self.dataset.length
        batch_size = len(loader)

        # pbar = tqdm(total=epoch_size)
        # pbar.set_description('Eval')

        for batch_idx, (x,y) in enumerate(loader):
        
            x = x.to('cuda')
            # pbar.update(x.size(0))

            with torch.no_grad():
                loss =self. _eval(x,False)

                if self.name in ['LSA_EN','LSA_MAF','LSA_SOS']:
                    val_nllk += self.loss.autoregression_loss.sum().item()
                    val_rec += self.loss.reconstruction_loss.sum().item()
                    # keep lambda = 1
                    val_loss = val_nllk + val_rec
                else:
                     val_loss += self.loss.total_loss.sum().item() 
                                    
        if self.name in ['LSA_EN','LSA_MAF','LSA_SOS']:
            print('Val_loss:{:.6f}\t Rec: {:.6f}\t Nllk: {:.6f}'.format(val_loss/epoch_size, val_rec/epoch_size, val_nllk/epoch_size))
        else:
            print('Val_loss:{:.6f}\t'.format(val_loss/epoch_size))
            
            # pbar.set_description('Val_loss: {:.6f}'.format(val_loss))
        
        # pbar.close()
                

        return val_loss/epoch_size, val_rec/epoch_size,val_nllk/epoch_size






    def train_one_class_classification(self, cl):
        # type: () -> None
        """
        Actually performs trains.
        """     

        best_validation_epoch = 0
        

        best_validation_loss = float('+inf')
        best_validation_rec = float('+inf')
        best_validation_nllk = float('+inf')

        best_model = None 
        best_rec_model =None
        old_validation_loss = float('+inf')

        history ={}
        history['val_loss'] =[]
        history['val_rec'] =[]
        history['val_nllk'] =[]

        history['trn_loss'] =[]
        history['trn_rec'] =[]
        history['trn_nllk'] =[]

        print(f"n_parameters:{self.model.n_parameters}")
        
        
        
        # if not(self.checkpoint == None): # start from that check point 
        #     self.model.load_w(self.model_dir)
        #     cknum =  int (self.checkpoint)
        #     print(f"Continue Training from {cknum}")
        # else:
        cknum = 0

        for epoch in range(self.train_epoch):

            epoch_new = epoch + cknum
            if self.mulobj:
                model_dir_epoch = join(self.checkpoints_dir,f'{cl}{self.name}_mul_{epoch_new}.pkl')

            else:
                model_dir_epoch = join(self.checkpoints_dir,f'{cl}{self.name}_{self.lam}_{epoch_new}.pkl')

            
            train_loss, train_rec, train_nllk= self.train_every_epoch(epoch,cl)
            
            # validate
            validation_loss,validation_rec,validation_nllk = self.validate(epoch, cl)

            old_validation_loss = validation_loss
            
               
            if (validation_loss < best_validation_loss): 
                best_validation_loss = validation_loss
                best_validation_epoch = epoch
                best_model = self.model 
                if (epoch>self.before_log_epochs):
                    torch.save(best_model.state_dict(), self.best_model_dir)

                print(f'Best_epoch at :{epoch} with valid_loss:{best_validation_loss}' )
                
                # if (validation_rec< best_validation_rec) and (validation_nllk<best_validation_nllk):
                #     torch.save(self.model.state_dict(),self.double_best_model_dir)
                #     double_best_validation_epoch = epoch
                #     best_validation_rec = validation_rec
                #     best_validation_nllk = validation_nllk
                #     print(f'Double_Best_epoch at :{epoch} with valid_loss:{best_validation_loss}')


            if (epoch % 100 == 0 ) :
                    torch.save(self.model.state_dict(), model_dir_epoch)
                    np.save(self.result_dir,history)
            
            

            # if (self.dataset.name =='cifar10') and (self.name=='LSA_SOS') and (epoch - double_best_validation_epoch)> 200:
            #     print (f"Break at Epoch:{epoch}")
            #     break
            if self.fixed and (not self.ae_finished):
                if (validation_rec < best_validation_rec): 
                    best_validation_rec = validation_rec
                    best_validation_rec_epoch = epoch
                    best_rec_model = self.model
                    torch.save(best_rec_model.state_dict(), f'{self.best_model_rec_dir}.pkl')
                    print(f'Best_epoch at :{epoch} with rec_loss:{best_validation_rec}' )
                
                if (epoch - best_validation_rec_epoch)> 30:
                    self.ae_finished = True  # autoencoder finished
                    # self.model.load_w(join(self.checkpoints_dir, f'{self.best_model_rec_dir}_{epoch}.pkl'))
            

            # record loss history
            history['val_loss'].append(validation_loss)
            history['val_rec'].append(validation_rec)
            history['val_nllk'].append(validation_nllk)
            
            history['trn_loss'].append(train_loss)
            history['trn_rec'].append(train_rec)
            history['trn_nllk'].append(train_nllk)

            

        print("Training finish! Normal_class:>>>>>", cl)
        

        torch.save(self.model.state_dict(), self.model_dir)
        
        np.save(self.result_dir,history)

    def test_one_class_classification(self):
        # type: () -> None
        """
        Actually performs tests.
        """
        # Prepare a table to show results
        oc_table = self.empty_table

        # Set up container for metrics from all classes
        all_metrics = []
        bs =self.batch_size
        quantile_flag = self.quantile_flag

        # Start iteration over classes
        for cl_idx, cl in enumerate(self.dataset.test_classes):
            self.cl = cl
            self.get_path()
            print(f"Testinng on {cl}")

            if self.trainflag:
            # train model
                # else:
                self.model.load_w(join(self.checkpoints_dir, f'{self.model.name}_start.pkl'))
                # if self.pretrained:
                #     # print("load pretrained model")
                #     self.model.load_w(f'checkpoints/{self.dataset.name}/combinedFalse/PtrFalse/FixTrue/b1h2048/{cl}{self.model.name}_1_rec_b.pkl.pkl')



                self.train_one_class_classification(cl)
            
            # Load the checkpoint    
            # if self.add:
            #     self.add_two_pretrained_model(cl)
            # else:
            self.model.load_w(self.model_dir)
            print(f"Load Model from {self.model_dir}")
        
            self.model.eval()

                # normalizing coefficient of the Novelty Score (Eq.9 in LSA)
            min_llk, max_llk, min_rec, max_rec,min_q1,max_q1,min_q2,max_q2,min_qinf,max_qinf = self.compute_normalizing_coefficients(cl)
            
            # Test sets
            self.dataset.test(cl,self.novel_ratio)
            data_num = self.dataset.length
            print(data_num)
            loader = DataLoader(self.dataset, batch_size = bs)

            sample_llk = np.zeros(shape=(len(self.dataset),))
            sample_rec = np.zeros(shape=(len(self.dataset),))
            sample_q1 = np.zeros(shape=(len(self.dataset),))
            sample_q2 = np.zeros(shape=(len(self.dataset),))
            sample_qinf = np.zeros(shape=(len(self.dataset),))
            
            sample_y = np.zeros(shape=(len(self.dataset),))

            for i, (x, y) in tqdm(enumerate(loader), desc=f'Computing scores for {self.dataset}'):
                
                x = x.to(self.device)

                with torch.no_grad():
                    if quantile_flag:
                       tot_loss, q1, q2, qinf = self._eval(x, average = False, quantile_flag =quantile_flag)
                    else:
                        tot_loss = self._eval(x,average = False,quantile_flag = quantile_flag)
                
                sample_y[i*bs:i*bs+bs] = y
                # score larger-->normal data
                if self.name in ['LSA','LSA_MAF','LSA_SOS','LSA_EN','LSA_QT','AAE']:
                    sample_rec[i*bs:i*bs+bs] = - self.loss.reconstruction_loss.cpu().numpy()
                    
                if self.name in ['LSA_MAF','LSA_SOS','LSA_EN','LSA_QT',
                'EN','SOS','MAF',
                'LSA_ET_QT','LSA_ET_EN','LSA_ET_MAF','LSA_ET_SOS']:    
                    sample_llk[i*bs:i*bs+bs] = - self.loss.autoregression_loss.cpu().numpy()
                
                if quantile_flag:
                    sample_q1[i*bs:i*bs+bs] = q1
                    sample_q2[i*bs:i*bs+bs] = q2
                    sample_qinf[i*bs:i*bs+bs] = qinf

            # +inf,-inf,nan
            sample_llk = modify_inf(sample_llk)

            llk1= np.dot(sample_llk,sample_y).sum()
            llk2 = sample_llk.sum()-llk1

            # llk1 should be larger than llk2
            llk1 =llk1/np.sum(sample_y)
            # average llk for normal examples
            llk2 =llk2/(data_num-np.sum(sample_y)) 
            # average llk for novel examples

            print(f'min_llk:{min_llk},max_llk:{max_llk}'
                    )
            print(f'min_rec:{min_rec},max_rec:{max_rec}')

            
            # Normalize scores
            sample_llk_n = normalize(sample_llk, min_llk, max_llk)
            sample_rec_n = normalize(sample_rec, min_rec, max_rec)

            
            #print(sample_llk)
            # Compute the normalized novelty score
            if self.score_normed:
                sample_rec = sample_rec_n
                sample_llk = sample_llk_n    
            
            sample_ns = novelty_score(sample_llk, sample_rec)
            sample_ns = modify_inf(sample_ns)

            # Compute precision, recall, f1_score based on threshold
            # if we know a/100 is the percentile of novelty samples in test set
            sample_ns_t = sample_ns
            # y = 1 normal, y = 0 novel
            real_nr= float(sum(sample_y==0)/len(sample_y))            
            print(f"Real Novelty_Num: {sum(sample_y==0)} in {len(sample_y)} samples, Novel Ratio= {real_nr}")


            threshold = np.percentile(sample_ns_t, 20)

            y_hat = np.where(sample_ns_t >= threshold, 1, 0)
            
            print(f"Predicted Novelty_Num: {sum(y_hat==0)} in {len(y_hat)} samples")

            
            ####################################################
            precision, recall, f1, _ = precision_recall_fscore_support((sample_y==0),(y_hat==0), average="binary")
            ####################################################

            
            ## metrics 
            this_class_metrics = [
            roc_auc_score(sample_y, sample_ns)    #
            ]

            if self.name in ['LSA_EN','LSA_SOS','LSA_MAF','LSA_QT']:
                this_class_metrics.append(
                roc_auc_score(sample_y, sample_llk))
                
                this_class_metrics.append(
                roc_auc_score(sample_y, sample_rec))

            this_class_metrics.append(precision)

            this_class_metrics.append(f1)

            this_class_metrics.append(recall)



            if self.name in ['LSA_EN','LSA_SOS','LSA_MAF','LSA_QT',
            'LSA_ET_EN','LSA_ET_SOS','LSA_ET_MAF','LSA_ET_QT','SOS']:

                this_class_metrics.append(llk1)
                this_class_metrics.append(llk2)

            # add metrics related to quantile 
            if quantile_flag:
                this_class_metrics.append(roc_auc_score(sample_y,sample_q1))
                this_class_metrics.append(roc_auc_score(sample_y,sample_q2))
                this_class_metrics.append(roc_auc_score(sample_y,sample_qinf))

            # write on table
            oc_table.add_row([cl_idx] + this_class_metrics)

            all_metrics.append(this_class_metrics)

            print(f"Class_AUC-{cl}:{roc_auc_score(sample_y, sample_ns)}")



        
        # Compute average AUROC and print table
        all_metrics = np.array(all_metrics)
        avg_metrics = np.mean(all_metrics, axis=0)
        oc_table.add_row(['avg'] + list(avg_metrics))
        print(oc_table)

        # Save table
        with open(self.output_file, mode='w') as f:
            f.write(str(oc_table))
     
    def compute_normalizing_coefficients(self,cl):
        # type: (int) -> Tuple[float, float, float, float]
        """
        Computes normalizing coeffients for the computation of the Novelty score (Eq. 9-10).
        :param cl: the class to be considered normal.
        :return: a tuple of normalizing coefficients in the form (llk_min, llk_max, rec_min, rec_max).
        """
        bs = self.batch_size
        quantile_flag = self.quantile_flag
        self.dataset.val(cl)
        loader = DataLoader(self.dataset, batch_size= bs)

        sample_llk = np.zeros(shape=(len(self.dataset),))
        sample_rec = np.zeros(shape=(len(self.dataset),))
        sample_q1 = np.zeros(shape=(len(self.dataset),))
        sample_q2 = np.zeros(shape=(len(self.dataset),))
        sample_qinf = np.zeros(shape=(len(self.dataset),))

        for i, (x, y) in enumerate(loader): 
            x = x.to(self.device)
            with torch.no_grad():
                if quantile_flag:
                    tot_loss, q1, q2, qinf = self._eval(x, average = False, quantile_flag =quantile_flag)
                    sample_q1[i*bs:i*bs+bs] = q1
                    sample_q2[i*bs:i*bs+bs] = q2
                    sample_qinf[i*bs:i*bs+bs] = qinf

                else:
                    tot_loss = self._eval(x,average = False,quantile_flag = quantile_flag)
                
            # score larger-->normal data
            if self.name in ['LSA','LSA_MAF','LSA_SOS','LSA_EN','LSA_QT']:
                sample_rec[i*bs:i*bs+bs] = - self.loss.reconstruction_loss.cpu().numpy()
            
            if self.name in ['LSA_MAF','LSA_SOS','LSA_EN','LSA_QT',
            'LSA_ET_EN','LSA_ET_MAF','LSA_ET_SOS','LSA_ET_QT',
            'EN','SOS','MAF']:    
                sample_llk[i*bs:i*bs+bs] = - self.loss.autoregression_loss.cpu().numpy()

            sample_llk = modify_inf(sample_llk)

        return sample_llk.min(), sample_llk.max(), sample_rec.min(), sample_rec.max(),sample_q1.min(),sample_q1.max(),sample_q2.min(),sample_q2.max(),sample_qinf.min(), sample_qinf.max()


    @property
    def empty_table(self):
        # type: () -> PrettyTable
        """
        Sets up a nice ascii-art table to hold results.
        This table is suitable for the one-class setting.
        :return: table to be filled with auroc metrics.
        """
        table = PrettyTable()
        if self.name in ['LSA_MAF','LSA_SOS','LSA_EN']:

            if self.quantile_flag:
                table.field_names = ['Class', 'AUROC-NS', 'AUROC-LLK', 'AUROC-REC','PRCISION','F1','RECALL','llk1','llk2'
                ,'AUROC-q1','AUROC-q2','AUROC-qinf']
            
            else:
            
                table.field_names = ['Class', 'AUROC-NS', 'AUROC-LLK', 'AUROC-REC','PRCISION','F1','RECALL','llk1','llk2'
                ]

        elif self.name in ['MAF','SOS','EN','LSA',]:
            
            if self.quantile_flag:
                table.field_names = ['Class', 'AUROC-NS','PRCISION','F1','RECALL','llk1','llk2','AUROC-q1','AUROC-q2','AUROC-qinf']

            else:
                 table.field_names = ['Class', 'AUROC-NS','PRCISION','F1','RECALL',
                ]

        elif self.name in ['LSA_ET_QT','LSA_ET_EN','LSA_ET_MAF','LSA_ET_SOS']:
            table.field_names = ['Class', 'AUROC-NS','PRCISION','F1','RECALL','llk1','llk2']
        # add new metric title related to quantile 
        

        # format
        table.float_format = '0.4'
        return table



# compute best threshold 
    def compute_threshold(self, cl):

        dataset = self.dataset
        dataset.val2(cl)

        loader = DataLoader(dataset)

        sample_score = np.zeros(shape=(len(loader),))
        ytrue = np.zeros(shape=(len(loader),))
        for i, (x, y) in enumerate(loader):
            x = x.to(self.device)

            if self.name == 'LSA':
                x_r = self.model(x)
                self.loss.lsa(x, x_r)

            elif self.name == 'LSA_EN':
                
                x_r, z, z_dist = self.model(x)
                self.loss.lsa_en(x, x_r, z, z_dist)
            
            elif self.name in ['LSA_SOS', 'LSA_MAF']:
                x_r, z, s, log_jacob_T_inverse = self.model(x)
                self.loss.lsa_flow(x,x_r,s,log_jacob_T_inverse)
            
            elif self.name in ['SOS', 'MAF']:
                s, log_jacob_T_inverse = self.model(x)
                self.loss.flow(s,log_jacob_T_inverse)
            
            elif self.name == 'EN':
                z_dist = model(x)
                self.loss.en(z_dist)

            sample_score[i] = - self.loss.total_loss # large score -- normal
            ytrue[i] =y.numpy()

        best_e = 0
        best_f = 0
        best_e_ = 0
        best_f_ = 0

        # real label y  normal 0,  novel 1

        # predict score  sample_score 
        # predict label y_hat
        minS = sample_score.min() - 0.1
        maxS = sample_score.max() + 0.1

        for e in np.arange(minS, maxS, 0.1):

            y_hat = np.less(sample_score, e) #  normal 0  novel1
            # # TP Predict novel as novel y =1, y_hat =1
            true_positive = np.sum(np.logical_and(y_hat, ytrue))
            # # FP Predict normal as novel y = 0, y_hat = 1
            # false_positive = np.sum(np.logical_and(y_hat, logical_not(y)))
            # # PN Predict novel as normal y =1, y_hat = 0
            # false_negative = np.sum(np.logical_and(np.logical_not(y_hat),y))
            if true_positive > 0:

                f1 = f1_score(ytrue, y_hat)
                if f1 > best_f:
                    best_f = f1
                    best_e = e
                if f1 >= best_f_:
                    best_f_ = f1
                    best_e_ = e

        best_e = (best_e + best_e_) / 2.0

        print("Best e: ", best_e)
        return best_e