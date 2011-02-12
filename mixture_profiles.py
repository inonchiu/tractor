# make mixture-of-Gaussian galaxy profiles

import matplotlib
matplotlib.use('Agg')
from math import pi as pi
import pylab as plt
import numpy as np
import scipy.optimize as op

# magic number
maxradius = 7.
# magic number setting what counts as stopping time
squared_deviation_scale = 1.e-6

exp_amp = np.array([2.68955313e-04, 8.37767155e-03, 1.01360468e-01,
                    7.08249585e-01, 2.61576535e+00, 2.80398456e+00])
exp_var = np.array([9.69266499e-04, 1.36248823e-02, 8.69290774e-02,
                    3.93552018e-01, 1.45353628e+00, 4.75412889e+00])
dev_amp = np.array([4.13073729e-07, 3.15318990e-05, 6.30620665e-04,
                    6.97615164e-03, 5.47379252e-02, 3.46617065e-01,
                    2.42820930e+00, 1.61889131e+02])
dev_var = np.array([3.21697566e-07, 5.16233300e-05, 1.07433204e-03,
                    1.21423795e-02, 9.84125685e-02, 6.49943728e-01,
                    4.17419470e+00, 1.04112953e+02])

class MixtureOfGaussians():

    # symmetrize is an unnecessary step in principle, but in practice?
    def __init__(self, amp, mean, var):
        self.amp = np.array(amp)
        self.mean = np.array(mean)
        (self.K, self.D) = self.mean.shape
        self.set_var(var)
        self.symmetrize()
        self.test()

    def __str__(self):
        result = "MixtureOfGaussians instance with %d components in %d dimensions:\n" % (self.K, self.D)
        result += " amp  = %s\n" % self.amp.__str__()
        result += " mean = %s\n" % self.mean.__str__()
        result += " var  = %s\n" % self.var.__str__()
        return result

    def set_var(self, var):
        if var.size == self.K:
            self.var = np.zeros((self.K, self.D, self.D))
            for d in range(self.D):
                self.var[:,d,d] = var
        else:
            self.var = np.array(var)

    def symmetrize(self):
        for i in range(self.D):
            for j in range(i):
                tmpij = 0.5 * (self.var[:,i,j] + self.var[:,j,i])
                self.var[:,i,j] = tmpij
                self.var[:,j,i] = tmpij

    def test(self):
        assert(self.amp.shape == (self.K, ))
        assert(self.mean.shape == (self.K, self.D))
        assert(self.var.shape == (self.K, self.D, self.D))

    def copy(self):
        return MixtureOfGaussians(self.amp, self.mean, self.var)

    def normalize(self):
        self.amp /= np.sum(self.amp)

    def extend(self, other):
        assert(self.D == other.D)
        self.K = self.K + other.K
        self.amp = np.append(self.amp, other.amp)
        self.mean = np.reshape(np.append(self.mean, other.mean), (self.K, self.D))
        self.var = np.reshape(np.append(self.var, other.var), (self.K, self.D, self.D))
        self.test

    # dstn: should this be called "correlate"?
    def convolve(self, other):
        assert(self.D == other.D)
        newK = self.K * other.K
        D = self.D
        newamp = np.zeros((newK))
        newmean = np.zeros((newK, D))
        newvar = np.zeros((newK, D, D))
        newk = 0
        for k in other.K:
            nextnewk = newk + self.K
            newamp[newk:nextnewk] = self.amp * other.amp[k]
            newmean[newk:nextnewk,:] = self.mean + other.mean[k]
            newvar[newk:nextnewk,:,:] = self.var * other.var[k]
            newk = nextnewk
        return MixtureOfGaussians(newamp, newmean, newvar)

    # ideally pos is a numpy array shape (N, self.D)
    # returns a numpy array shape (N)
    # may fail for self.D == 1
    # loopy
    def evaluate(self, pos):
        if pos.size == self.D:
            pos = np.reshape(pos, (1, self.D))
        (N, D) = pos.shape
        assert(self.D == D)
        twopitotheD = (2.*np.pi)**self.D
        result = np.zeros(N)
        for k in range(self.K):
            dpos = pos - self.mean[k]
            dsq = np.sum(pos * np.dot(dpos, np.linalg.inv(self.var[k])),axis=1)
            result += (self.amp[k] / np.sqrt(twopitotheD * np.linalg.det(self.var[k]))) * np.exp(-0.5 * dsq)
        return result

# note wacky normalization because this is for 2-d Gaussians
# (but only ever called in 1-d).  Wacky!
def not_normal(x, m, V):
    return 1. / (2. * pi * V) * np.exp(-0.5 * x**2 / V)

def hogg_dev(x):
    return np.exp(-1. * (x**0.25))

def mixture_of_not_normals(x, pars):
    K = len(pars)/2
    y = 0.
    for k in range(K):
        y += pars[k] * not_normal(x, 0., pars[k+K])
    return y

# note that you can do (x * ymix - x * ytrue)**2 or (ymix - ytrue)**2
# each has disadvantages.
def badness_of_fit_exp(lnpars):
    pars = np.exp(lnpars)
    x = np.arange(0., maxradius, 0.01)
    return np.mean((mixture_of_not_normals(x, pars)
                    - np.exp(-x))**2) / squared_deviation_scale

# note that you can do (x * ymix - x * ytrue)**2 or (ymix - ytrue)**2
# each has disadvantages.
def badness_of_fit_dev(lnpars):
    pars = np.exp(lnpars)
    x = np.arange(0., maxradius, 0.001)
    return np.mean((mixture_of_not_normals(x, pars)
                    - hogg_dev(x))**2) / squared_deviation_scale

def optimize_mixture(K, pars, model):
    if model == 'exp':
        func = badness_of_fit_exp
    if model == 'dev':
        func = badness_of_fit_dev
    print pars
    newlnpars = op.fmin_bfgs(func, np.log(pars), maxiter=300)
    print np.exp(newlnpars)
    return (func(newlnpars), np.exp(newlnpars))

def plot_mixture(pars, fn, model):
    x1 = np.arange(0., maxradius, 0.001)
    if model == 'exp':
        y1 = np.exp(-x1)
        badness = badness_of_fit_exp(np.log(pars))
    if model == 'dev':
        y1 = hogg_dev(x1)
        badness = badness_of_fit_dev(np.log(pars))
    x2 = np.arange(0., maxradius+2., 0.001)
    y2 = mixture_of_not_normals(x2, pars)
    plt.clf()
    plt.plot(x1, y1, 'k-')
    plt.plot(x2, y2, 'k-', lw=4, alpha=0.5)
    plt.xlim(-0.5, np.max(x2))
    plt.ylim(-0.1*np.max(y1), 1.1*np.max(y1))
    plt.title(r"K = %d / mean-squared deviation = $%f\times 10^{-6}$" % (len(pars)/2, badness))
    plt.savefig(fn)

def rearrange_pars(pars):
    K = len(pars) / 2
    indx = np.argsort(pars[K:K+K])
    amp = pars[indx]
    var = pars[K+indx]
    return np.append(amp, var)

# run this (possibly with adjustments to the magic numbers at top)
# to find different or better mixtures approximations
def optimize_mixtures():
    for model in ['exp', 'dev']:
        amp = np.array([1.0])
        var = np.array([1.0])
        pars = np.append(amp, var)
        (badness, pars) = optimize_mixture(1, pars, model)
        lastKbadness = badness
        bestbadness = badness
        for K in range(2,20):
            print 'working on K = %d' % K
            newvar = 0.5 * np.min(np.append(var,1.0))
            newamp = 1.0 * newvar
            amp = np.append(newamp, amp)
            var = np.append(newvar, var)
            pars = np.append(amp, var)
            for i in range(100):
                (badness, pars) = optimize_mixture(K, pars, model)
                if badness < bestbadness:
                    print '%d %d improved' % (K, i)
                    bestpars = pars
                    bestbadness = badness
                else:
                    print '%d %d not improved' % (K, i)
                    var[0] = 0.5 * var[np.random.randint(K)]
                    amp[0] = 1.0 * var[0]
                    pars = np.append(amp, var)
                if (bestbadness < 0.5 * lastKbadness) and (i > 5):
                    print '%d %d improved enough' % (K, i)
                    break
            lastKbadness = bestbadness
            pars = rearrange_pars(bestpars)
            plot_mixture(pars, 'K%02d_%s.png' % (K, model), model)
            amp = pars[0:K]
            var = pars[K:K+K]
            if bestbadness < 1.:
                print model
                print pars
                break

def functional_test_circular_mixtures():
    exp_mixture = MixtureOfGaussians(exp_amp, np.zeros((exp_amp.size, 2)), exp_var)
    dev_mixture = MixtureOfGaussians(dev_amp, np.zeros((dev_amp.size, 2)), dev_var)
    pos = np.random.uniform(-5.,5.,size=(24,2))
    exp_eva = exp_mixture.evaluate(pos)
    dev_eva = dev_mixture.evaluate(pos)
    (N, D) = pos.shape
    for n in range(N):
        print '(%+6.3f %+6.3f) exp: %+8.5f' % (pos[n,0], pos[n,1], exp_eva[n] - np.exp(-1. * np.sqrt(np.sum(pos[n] * pos[n]))))
        print '(%+6.3f %+6.3f) dev: %+8.5f' % (pos[n,0], pos[n,1], dev_eva[n] - np.exp(-1. * np.sqrt(np.sum(pos[n] * pos[n]))**0.25))

if __name__ == '__main__':
    functional_test_circular_mixtures()
