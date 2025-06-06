import os
import math
from timeit import default_timer
from tqdm import tqdm
import numpy as np

import torch
import ml_collections

import matplotlib.pyplot as plt
import matplotlib.animation as animation

import scipy.stats as stats

def navier_stokes_2d(w0, f, visc, T, delta_t=1e-4, record_steps=1):

    # Grid size - must be power of 2
    N = w0.size()[-1]

    # Maximum frequency
    k_max = math.floor(N/2.0)

    # Number of steps to final time
    steps = math.ceil(T/delta_t)

    # Initial vorticity to Fourier space
    w_h = torch.fft.rfft2(w0)

    # Forcing to Fourier space
    f_h = torch.fft.rfft2(f)

    # If same forcing for the whole batch
    if len(f_h.size()) < len(w_h.size()):
        f_h = torch.unsqueeze(f_h, 0)

    # Record solution every this number of steps
    record_time = math.floor(steps/record_steps)
    # record_times = [steps, steps - 1]

    # Wavenumbers in y-direction
    k_y = torch.cat((torch.arange(start=0, end=k_max, step=1, device=w0.device), torch.arange(
        start=-k_max, end=0, step=1, device=w0.device)), 0).repeat(N, 1)
    # Wavenumbers in x-direction
    k_x = k_y.transpose(0, 1)

    # Truncate redundant modes
    k_x = k_x[..., :k_max + 1]
    k_y = k_y[..., :k_max + 1]

    # Negative Laplacian in Fourier space
    lap = 4*(math.pi**2)*(k_x**2 + k_y**2)
    lap[0, 0] = 1.0
    # Dealiasing mask
    dealias = torch.unsqueeze(torch.logical_and(torch.abs(k_y) <= (
        2.0/3.0)*k_max, torch.abs(k_x) <= (2.0/3.0)*k_max).float(), 0)

    # Saving solution and time
    sol = torch.zeros(*w0.size(), record_steps, device=w0.device)
    sol_t = torch.zeros(record_steps, device=w0.device)

    sol_vel_x = torch.zeros(*w0.size(), record_steps, device=w0.device)
    sol_vel_y = torch.zeros(*w0.size(), record_steps, device=w0.device)

    # Record counter
    c = 0
    # Physical time
    t = 0.0
    for j in tqdm(range(steps)):
        # Stream function in Fourier space: solve Poisson equation
        psi_h = w_h / lap

        # Velocity field in x-direction = psi_y
        q = 2. * math.pi * k_y * 1j * psi_h
        q = torch.fft.irfft2(q, s=(N, N))

        # Velocity field in y-direction = -psi_x
        v = -2. * math.pi * k_x * 1j * psi_h
        v = torch.fft.irfft2(v, s=(N, N))

        # Partial x of vorticity
        w_x = 2. * math.pi * k_x * 1j * w_h
        w_x = torch.fft.irfft2(w_x, s=(N, N))

        # Partial y of vorticity
        w_y = 2. * math.pi * k_y * 1j * w_h
        w_y = torch.fft.irfft2(w_y, s=(N, N))

        # Non-linear term (u.grad(w)): compute in physical space then back to Fourier space
        F_h = torch.fft.rfft2(q*w_x + v*w_y)

        # Dealias
        F_h = dealias * F_h

        # Crank-Nicolson update
        w_h = (-delta_t*F_h + delta_t*f_h + (1.0 - 0.5*delta_t*visc*lap)
               * w_h)/(1.0 + 0.5*delta_t*visc*lap)

        # Update real time (used only for recording)
        t += delta_t

        if (j+1) % record_time == 0:
            # if (j + 1) in record_times:
            # Solution in physical space
            w = torch.fft.irfft2(w_h, s=(N, N))

            # Record solution and time
            sol[..., c] = w
            sol_t[c] = t

            sol_vel_x[..., c] = q
            sol_vel_y[..., c] = v

            c += 1

    return sol, sol_t, sol_vel_x, sol_vel_y


class GaussianRF:
    def __init__(self, dim, size, alpha=2, tau=3, sigma=None, boundary="periodic", device=None):

        self.dim = dim
        self.device = device

        if sigma is None:
            sigma = tau**(0.5*(2*alpha - self.dim))

        k_max = size//2

        if dim == 1:
            k = torch.cat((torch.arange(start=0, end=k_max, step=1, device=device),
                           torch.arange(start=-k_max, end=0, step=1, device=device)), 0)

            self.sqrt_eig = size * \
                math.sqrt(2.0)*sigma * \
                ((4*(math.pi**2)*(k**2) + tau**2)**(-alpha/2.0))
            self.sqrt_eig[0] = 0.0

        elif dim == 2:
            wavenumers = torch.cat((torch.arange(start=0, end=k_max, step=1, device=device),
                                    torch.arange(start=-k_max, end=0, step=1, device=device)), 0).repeat(size, 1)

            k_x = wavenumers.transpose(0, 1)
            k_y = wavenumers

            self.sqrt_eig = (size**2)*math.sqrt(2.0)*sigma * \
                ((4*(math.pi**2)*(k_x**2 + k_y**2) + tau**2)**(-alpha/2.0))
            self.sqrt_eig[0, 0] = 0.0

        elif dim == 3:
            wavenumers = torch.cat((torch.arange(start=0, end=k_max, step=1, device=device),
                                    torch.arange(start=-k_max, end=0, step=1, device=device)), 0).repeat(size, size, 1)

            k_x = wavenumers.transpose(1, 2)
            k_y = wavenumers
            k_z = wavenumers.transpose(0, 2)

            self.sqrt_eig = (size**3)*math.sqrt(2.0)*sigma*((4*(math.pi**2)
                                                             * (k_x**2 + k_y**2 + k_z**2) + tau**2)**(-alpha/2.0))
            self.sqrt_eig[0, 0, 0] = 0.0

        self.size = []
        for j in range(self.dim):
            self.size.append(size)

        self.size = tuple(self.size)

    def sample(self, N):

        coeff = torch.randn(
            N, *self.size, dtype=torch.cfloat, device=self.device)
        coeff = self.sqrt_eig * coeff

        # print(f"sqrt_eig: {self.sqrt_eig.shape}")
        # print(self.sqrt_eig)
        # plt.imshow(np.log(self.sqrt_eig.detach().cpu().numpy() ) )
        # plt.colorbar()
        # plt.title('sqrt_eig')
        # plt.show()
        

        return torch.fft.ifftn(coeff, dim=list(range(-1, -self.dim - 1, -1))).real

def generate_ns_data(configs):
    # path = os.path.join('..', 'data', 'navier_stokes',
                    # f'NavierStokes_v_{configs.viscosity}_N_{configs.N}_T_{int(configs.T)}_nx_{configs.nx}_ny_{configs.ny}')

    path = configs.path #os.path.join('temp_data')
        
    print(torch.cuda.get_device_name(0))
    device = torch.device('cuda')
    
    # Resolution
    # s = configs.nx if configs.viscosity >= 1e-03 else configs.nx * 4 
    s = configs.nx
    
    # Number of solutions to generate                                     
    N = configs.N                         
    delta_t = configs.delta_t

    # Set up 2d GRF with covariance parameters                   
    GRF = GaussianRF(2, s, alpha=configs.alpha, tau=configs.tau, device=device)  
    
    # Forcing function: 0.1*(sin(2pi(x+y)) + cos(2pi(x+y)))
    t = torch.linspace(0, 1, s+1, device=device)             
    t = t[0:-1]
    
    X, Y = torch.meshgrid(t, t, indexing='ij')
    f = 0.1*(torch.sin(2*math.pi*(X + Y)) + torch.cos(2*math.pi*(X + Y)))

    # Number of snapshots from solution
    record_steps = configs.time_steps_inference
    
    
    # Batch size
    bsize = configs.batch_size #1 #100

    # Inputs
    times = torch.zeros((bsize, record_steps))
    
    a = np.zeros((bsize, s, s))
    
    # Solutions
    u = np.zeros((bsize, s, s, record_steps))

    vx = np.zeros((bsize, s, s, record_steps))
    vy = np.zeros((bsize, s, s, record_steps))

    c = 0
    viscosity = configs.viscosity
    T = configs.T
    t0 = default_timer()
    
    for j in range(N//bsize):
        np.random.seed(j)
        # Sample random feilds
        w0 = GRF.sample(bsize)

        # Solve NS
        sol, sol_t, sol_vel_x, sol_vel_y = navier_stokes_2d(w0, f, viscosity, T, delta_t, record_steps)

        a[...] = w0.cpu().numpy()
        u[...] = sol.cpu().numpy()
        times[...] = sol_t

        vx[...] = sol_vel_x.cpu().numpy()
        vy[...] = sol_vel_y.cpu().numpy()

        del w0
        del sol
        del sol_vel_x
        del sol_vel_y
        torch.cuda.empty_cache()

        c += bsize
        t1 = default_timer()
        print(j, c, t1-t0)

    # if configs.viscosity < 1e-03:
    #     a = a[:, ::4, ::4]
    #     u = u[:, ::4, ::4, :]

        temp_path = os.path.join(path, f"{j}")

        os.makedirs(temp_path, exist_ok=True)
        np.save(os.path.join(temp_path, 'x.npy'), a)
        np.save(os.path.join(temp_path, 'y.npy'), u)
        np.save(os.path.join(temp_path, 't.npy'), times)
    
        np.save(os.path.join(temp_path, 'vx.npy'), vx)
        np.save(os.path.join(temp_path, 'vy.npy'), vy)


config = ml_collections.ConfigDict()

# Data configs
config.scenario = "navier_stokes"

config.alpha = 1.5
config.tau   = 14


config.viscosity = 1e-05
config.reynolds_number = int(
    np.sqrt(0.1) / ((2 * np.pi) ** (3/2) * config.viscosity))

config.N = 2
config.delta_t = 5*1e-05

if config.N < 50:
    config.batch_size = config.N
else:
    config.batch_size = 50

config.T = 50
config.noise_level = 0.

# Grid configs
config.nx = 512 * 1
config.ny = 512 * 1


config.time_steps_inference = 200


config.path = os.path.join(f'10_alpha_{config.alpha}_tau_{config.tau}_re_{config.reynolds_number}_N_{config.N}_T_{config.T}_nt_{config.time_steps_inference}_nx_{config.nx}')

config.path


generate_ns_data(config)
