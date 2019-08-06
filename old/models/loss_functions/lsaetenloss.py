import torch
import torch.nn as nn

from models.loss_functions.autoregression_loss import AutoregressionLoss


class LSAETENLoss(nn.Module):
    """
    Implements the loss of a LSA model.
    It is a sum of the reconstruction loss and the autoregression loss.
    """
    def __init__(self, cpd_channels):
        # type: (int, float) -> None
        """
        Class constructor.

        :param cpd_channels: number of bins in which the multinomial works.
        :param lam: weight of the autoregression loss.
        """
        super(LSAETENLoss, self).__init__()

        self.cpd_channels = cpd_channels
        
        # Set up loss modules
        self.autoregression_loss_fn = AutoregressionLoss(cpd_channels)

        # Numerical variables
        self.autoregression_loss = None
        self.total_loss = None

    def forward(self, z, z_dist,average = True):
        # type: (torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor) -> torch.Tensor
        """
        Forward propagation.

        :param z: the batch of latent representations.
        :param z_dist: the batch of estimated cpds.
        :return: the loss of the model (averaged along the batch axis).
        """
        # Compute pytorch loss
        arg_loss = self.autoregression_loss_fn(z, z_dist,average)
        tot_loss = arg_loss

        # Store numerical
        self.autoregression_loss = arg_loss
        self.total_loss = tot_loss

        return tot_loss