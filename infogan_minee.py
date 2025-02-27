import argparse
import os
import numpy as np
import math
import itertools

import torchvision.transforms as transforms
from torchvision.utils import save_image

from torch.utils.data import DataLoader
from torch.nn.utils import clip_grad_norm
from torchvision import datasets
from torch.autograd import Variable

import torch.nn as nn
import torch.nn.functional as F
import torch

from minee_conv import MineeConv

import matplotlib.pyplot as plt
mine_hidden_size = 200



os.makedirs("minee_images/static/", exist_ok=True)
os.makedirs("minee_images/varying_c1/", exist_ok=True)
os.makedirs("minee_images/varying_c2/", exist_ok=True)

parser = argparse.ArgumentParser()
parser.add_argument("--n_epochs", type=int, default=200, help="number of epochs of training")
parser.add_argument("--batch_size", type=int, default=64, help="size of the batches")
parser.add_argument("--lr", type=float, default=0.0001, help="adam: learning rate")
parser.add_argument("--b1", type=float, default=0.5, help="adam: decay of first order momentum of gradient")
parser.add_argument("--b2", type=float, default=0.999, help="adam: decay of first order momentum of gradient")
parser.add_argument("--n_cpu", type=int, default=8, help="number of cpu threads to use during batch generation")
parser.add_argument("--latent_dim", type=int, default=62, help="dimensionality of the latent space")
parser.add_argument("--code_dim", type=int, default=2, help="latent code")
parser.add_argument("--n_classes", type=int, default=10, help="number of classes for dataset")
parser.add_argument("--img_size", type=int, default=32, help="size of each image dimension")
parser.add_argument("--channels", type=int, default=1, help="number of image channels")
parser.add_argument("--sample_interval", type=int, default=400, help="interval between image sampling")
opt = parser.parse_args()
print(opt)

cuda = True if torch.cuda.is_available() else False


def weights_init_normal(m):
    classname = m.__class__.__name__
    if isinstance(m, nn.Conv2d):
        torch.nn.init.normal_(m.weight.data, 0.0, 0.02)
    elif isinstance(m, nn.BatchNorm2d):
        torch.nn.init.normal_(m.weight.data, 1.0, 0.02)
        torch.nn.init.constant_(m.bias.data, 0.0)


def to_categorical(y, num_columns):
    """Returns one-hot encoded Variable"""
    y_cat = np.zeros((y.shape[0], num_columns))
    y_cat[range(y.shape[0]), y] = 1.0

    return Variable(FloatTensor(y_cat))


def _uniform_sample(data, batch_size):
    # Sample the reference uniform distribution
    data_min = data.min(dim=0)[0]
    data_max = data.max(dim=0)[0]
    return (data_max - data_min) * torch.rand((batch_size, data_min.shape[0])) + data_min



class Generator(nn.Module):
    def __init__(self):
        super(Generator, self).__init__()
        input_dim = opt.latent_dim + opt.n_classes + opt.code_dim

        self.init_size = opt.img_size // 4  # Initial size before upsampling
        self.l1 = nn.Sequential(nn.Linear(input_dim, 128 * self.init_size ** 2))

        self.conv_blocks = nn.Sequential(
            nn.BatchNorm2d(128),
            nn.Upsample(scale_factor=2),
            nn.Conv2d(128, 128, 3, stride=1, padding=1),
            nn.BatchNorm2d(128, 0.8), 
            nn.ReLU(inplace=True),
            nn.Upsample(scale_factor=2),
            nn.Conv2d(128, 64, 3, stride=1, padding=1),
            nn.BatchNorm2d(64, 0.8), 
            nn.ReLU(inplace=True),
            nn.Conv2d(64, opt.channels, 3, stride=1, padding=1),
            nn.Tanh(),
        )

    def forward(self, noise, labels, code):
        gen_input = torch.cat((noise, labels, code), -1)
        out = self.l1(gen_input)
        out = out.view(out.shape[0], 128, self.init_size, self.init_size)
        img = self.conv_blocks(out)
        return img


class Discriminator(nn.Module):
    def __init__(self):
        super(Discriminator, self).__init__()

        def discriminator_block(in_filters, out_filters, bn=True):
            """Returns layers of each discriminator block"""
            block = [nn.Conv2d(in_filters, out_filters, 3, 2, 1), nn.ReLU(inplace=True)]
            if bn:
                block.append(nn.BatchNorm2d(out_filters, 0.8))
            return block

        self.conv_blocks = nn.Sequential(
            *discriminator_block(opt.channels, 16, bn=False),
            *discriminator_block(16, 32),
            *discriminator_block(32, 64),
            *discriminator_block(64, 128),
        )

        # The height and width of downsampled image
        ds_size = opt.img_size // 2 ** 4

        # Output layers
        self.adv_layer = nn.Sequential(nn.Linear(128 * ds_size ** 2, 1))

    def forward(self, img):
        out = self.conv_blocks(img)
        out = out.view(out.shape[0], -1)
        validity = torch.sigmoid(self.adv_layer(out))
        return validity


# Loss functions
adversarial_loss = torch.nn.BCELoss()

# Loss weights
lambda_con = 1

# Initialize generator and discriminator
generator = Generator()
discriminator = Discriminator()
mine_conv = MineeConv(channels=opt.channels, 
                     img_size=opt.img_size, 
                     code_size=opt.code_dim, 
                     discrete_code_size=opt.n_classes, 
                     hidden_size=mine_hidden_size)
# print(generator)
# print(generator.weight)
# print(mine_conv)
# print(mine_conv.weight)

if cuda:
    generator.cuda()
    discriminator.cuda()
    mine_conv.cuda()
    adversarial_loss.cuda()

# Initialize weights
generator.apply(weights_init_normal)
discriminator.apply(weights_init_normal)
mine_conv.apply(weights_init_normal)

# Configure data loader
os.makedirs("data/mnist", exist_ok=True)
dataloader = torch.utils.data.DataLoader(
    datasets.MNIST(
        "data/mnist",
        train=True,
        download=True,
        transform=transforms.Compose(
            [transforms.Resize(opt.img_size), transforms.ToTensor(), transforms.Normalize([0.5], [0.5])]
        ),
    ),
    batch_size=opt.batch_size,
    shuffle=True,
)


# Optimizers
optimizer_G = torch.optim.Adam(generator.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))
optimizer_D = torch.optim.Adam(discriminator.parameters(), lr=opt.lr, betas=(opt.b1, opt.b2))
optimizer_info = torch.optim.Adam(
    itertools.chain(generator.parameters(), mine_conv.parameters()), lr=opt.lr, betas=(opt.b1, opt.b2)
)

FloatTensor = torch.cuda.FloatTensor if cuda else torch.FloatTensor
LongTensor = torch.cuda.LongTensor if cuda else torch.LongTensor

# Static generator inputs for sampling
static_z = Variable(FloatTensor(np.zeros((opt.n_classes ** 2, opt.latent_dim))))
static_label = to_categorical(
    np.array([num for _ in range(opt.n_classes) for num in range(opt.n_classes)]), num_columns=opt.n_classes
)
static_code = Variable(FloatTensor(np.zeros((opt.n_classes ** 2, opt.code_dim))))


def sample_image(n_row, batches_done):
    """Saves a grid of generated digits ranging from 0 to n_classes"""
    # Static sample
    z = Variable(FloatTensor(np.random.normal(0, 1, (n_row ** 2, opt.latent_dim))))
    static_sample = generator(z, static_label, static_code)
    save_image(static_sample.data, "minee_images/static/%d.png" % batches_done, nrow=n_row, normalize=True)

    # Get varied c1 and c2
    zeros = np.zeros((n_row ** 2, 1))
    c_varied = np.repeat(np.linspace(-1, 1, n_row)[:, np.newaxis], n_row, 0)
    c1 = Variable(FloatTensor(np.concatenate((c_varied, zeros), -1)))
    c2 = Variable(FloatTensor(np.concatenate((zeros, c_varied), -1)))
    sample1 = generator(static_z, static_label, c1)
    sample2 = generator(static_z, static_label, c2)
    save_image(sample1.data, "minee_images/varying_c1/%d.png" % batches_done, nrow=n_row, normalize=True)
    save_image(sample2.data, "minee_images/varying_c2/%d.png" % batches_done, nrow=n_row, normalize=True)


# ----------
#  Training
# ----------
import pandas as pd
results_df = pd.DataFrame(columns=['batch', 'D loss', 'G loss', 'H(G(z,c)) loss', 'H(c, G(z,c)) loss', 'H(G(z,c) loss - H(c, G(z,c)) loss)'])

for epoch in range(opt.n_epochs):
    for i, (imgs, _) in enumerate(dataloader):

        batch_size = imgs.shape[0]

        # Adversarial ground truths
        valid = Variable(FloatTensor(batch_size, 1).fill_(1.0), requires_grad=False)
        fake = Variable(FloatTensor(batch_size, 1).fill_(0.0), requires_grad=False)

        # Configure input
        real_imgs = Variable(imgs.type(FloatTensor))

        # -----------------
        #  Train Generator
        # -----------------

        optimizer_G.zero_grad()

        # Sample noise and labels as generator input
        z = Variable(FloatTensor(np.random.normal(0, 1, (batch_size, opt.latent_dim))))
        label_input = to_categorical(np.random.randint(0, opt.n_classes, batch_size), num_columns=opt.n_classes)
        code_input = Variable(FloatTensor(np.random.uniform(-1, 1, (batch_size, opt.code_dim))))

        # Generate a batch of images
        gen_imgs = generator(z, label_input, code_input)

        # Loss measures generator's ability to fool the discriminator
        validity = discriminator(gen_imgs)
        g_loss = adversarial_loss(validity, valid)

        g_loss.backward()
        total_generator_grad_norm = 0
        for p in generator.parameters():
            param_norm = p.grad.data.norm(2)
            total_generator_grad_norm += param_norm.item() ** 2
        total_generator_grad_norm = total_generator_grad_norm ** (1/2)
        optimizer_G.step()

        # ---------------------
        #  Train Discriminator
        # ---------------------

        optimizer_D.zero_grad()

        # Loss for real images
        real_pred = discriminator(real_imgs)
        d_real_loss = adversarial_loss(real_pred, valid)

        # Loss for fake images
        fake_pred = discriminator(gen_imgs.detach())
        d_fake_loss = adversarial_loss(fake_pred, fake)

        # Total discriminator loss
        d_loss = (d_real_loss + d_fake_loss) / 2

        d_loss.backward()
        optimizer_D.step()

        # ------------------
        # Information Loss
        # ------------------

        optimizer_info.zero_grad()

        # Sample noise, labels and code as generator input
        z = Variable(FloatTensor(np.random.normal(0, 1, (batch_size, opt.latent_dim))))
        # discrete c
        sampled_labels = np.random.randint(0, opt.n_classes, batch_size)
        batch_label_input = to_categorical(sampled_labels, num_columns=opt.n_classes)
        # c
        batch_code = Variable(FloatTensor(np.random.uniform(-1, 1, (batch_size, opt.code_dim))))
        # G(z, c)
        batch_img = generator(z, batch_label_input, batch_code)

        # c reference
        batch_code_reference = Variable(FloatTensor(np.random.uniform(-1, 1, (batch_size, opt.code_dim))))
        batch_code_reference_2 = Variable(FloatTensor(np.random.uniform(-1, 1, (batch_size, opt.code_dim))))
        # discrete c reference
        sampled_labels_reference = np.random.randint(0, opt.n_classes, batch_size)
        batch_label_input_reference = to_categorical(sampled_labels_reference, num_columns=opt.n_classes)
        # discrete c reference
        sampled_labels_reference_2 = np.random.randint(0, opt.n_classes, batch_size)
        batch_label_input_reference_2 = to_categorical(sampled_labels_reference_2, num_columns=opt.n_classes)
        # z reference
        z_marginal = Variable(FloatTensor(np.random.uniform(0, 1, (batch_size, opt.latent_dim))))
        # G(z, c) marginal
        batch_img_marginal = generator(z_marginal, batch_label_input_reference_2, batch_code_reference_2) 

        batch_entropy_X_loss, batch_entropy_XY_loss = mine_conv(img=batch_img, 
                                                                code=batch_code, 
                                                                discrete_code=batch_label_input,
                                                                img_marginal=batch_img_marginal, 
                                                                code_marginal=batch_code_reference,
                                                                discrete_code_marginal=batch_label_input_reference)
        batch_average_entropy_loss = lambda_con * ((batch_entropy_X_loss + batch_entropy_XY_loss) / 2)
        batch_average_entropy_loss.backward()
        total_information_grad_norm = 0
        for p in generator.parameters():
            param_norm = p.grad.data.norm(2)
            total_information_grad_norm += param_norm.item() ** 2
        total_information_grad_norm = total_information_grad_norm ** (1/2)
        # adaptive gradient clipping
        clip_grad_norm(generator.parameters(), min(total_generator_grad_norm, total_information_grad_norm))
        optimizer_info.step()


        # --------------
        # Log Progress
        # --------------

        print(
            "[Epoch %d/%d] [Batch %d/%d] [D loss: %f] [G loss: %f] [H(G(z,c)) loss: %f] [H(c, G(z,c)) loss: %f] [H(G(z,c) loss - H(c, G(z,c)) loss): %f]"
            % (epoch, opt.n_epochs, i, len(dataloader), d_loss.item(), g_loss.item(), 
            batch_entropy_X_loss.item(), batch_entropy_XY_loss.item(), 
            batch_entropy_X_loss.item() - batch_entropy_XY_loss.item()
            )
        )
        batches_done = epoch * len(dataloader) + i
        results_df.loc[batches_done] = [batches_done, d_loss.item(), g_loss.item(), 
            batch_entropy_X_loss.item(), batch_entropy_XY_loss.item(), 
            batch_entropy_X_loss.item() - batch_entropy_XY_loss.item()]
        if batches_done % opt.sample_interval == 0:
            sample_image(n_row=10, batches_done=batches_done)

results_df.to_csv('minee_images/results.csv')
lines = results_df.drop(['batch'], axis=1).plot.line()
plt.savefig('minee_images/all_loss.png')
lines = results_df[['D loss', 'G loss']].plot.line()
plt.savefig('minee_images/gan_loss.png')
lines = results_df[['H(G(z,c) loss - H(c, G(z,c)) loss)']].plot.line()
plt.savefig('minee_images/H(G(z,c) loss - H(c, G(z,c)) loss).png')
