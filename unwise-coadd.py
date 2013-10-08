#! /usr/bin/env python

import matplotlib
matplotlib.use('Agg')
import numpy as np
import pylab as plt
import os
import sys
import tempfile
from scipy.ndimage.morphology import binary_dilation
from scipy.ndimage.measurements import label, center_of_mass
import datetime

import fitsio

if __name__ == '__main__':
    arr = os.environ.get('PBS_ARRAYID')
    d = os.environ.get('PBS_O_WORKDIR')
    if arr is not None and d is not None:
        os.chdir(d)
        sys.path.append(os.getcwd())

from astrometry.util.file import *
from astrometry.util.fits import *
from astrometry.util.multiproc import *
from astrometry.util.plotutils import *
from astrometry.util.miscutils import *
from astrometry.util.util import *
from astrometry.util.resample import *
from astrometry.util.run_command import *
from astrometry.libkd.spherematch import *
from astrometry.util.starutil_numpy import *

from astrometry.blind.plotstuff import *

from tractor import *
from tractor.ttime import *

from wise3 import get_l1b_file

import logging
lvl = logging.INFO
logging.basicConfig(level=lvl, format='%(message)s', stream=sys.stdout)

#median_f = np.median
median_f = flat_median_f

# GLOBALS:
# WISE Level 1b inputs
wisedir = 'wise-frames'
mask_gz = True
unc_gz = True


class Duck():
    pass

def get_coadd_tile_wcs(ra, dec, W, H, pixscale):
    '''
    Returns a Tan WCS object at the given RA,Dec center, axis aligned, with the
    given pixel W,H and pixel scale in arcsec/pixel.
    '''
    cowcs = Tan(ra, dec, (W+1)/2., (H+1)/2.,
                -pixscale/3600., 0., 0., pixscale/3600., W, H)
    return cowcs

def walk_wcs_boundary(wcs, step=1024, margin=0):
    '''
    Walk the image boundary counter-clockwise.
    '''
    W = wcs.get_width()
    H = wcs.get_height()
    xlo = 1
    xhi = W
    ylo = 1
    yhi = H
    if margin:
        xlo -= margin
        ylo -= margin
        xhi += margin
        yhi += margin
    
    xx,yy = [],[]
    xwalk = np.linspace(xlo, xhi, int(np.ceil((1+xhi-xlo)/float(step)))+1)
    ywalk = np.linspace(ylo, yhi, int(np.ceil((1+yhi-ylo)/float(step)))+1)
    # bottom edge
    x = xwalk[:-1]
    y = ylo
    xx.append(x)
    yy.append(np.zeros_like(x) + y)
    # right edge
    x = xhi
    y = ywalk[:-1]
    xx.append(np.zeros_like(y) + x)
    yy.append(y)
    # top edge
    x = list(reversed(xwalk))[:-1]
    y = yhi
    xx.append(x)
    yy.append(np.zeros_like(x) + y)
    # left edge
    x = xlo
    y = list(reversed(ywalk))[:-1]
    # (note, NOT closed)
    xx.append(np.zeros_like(y) + x)
    yy.append(y)
    #
    rr,dd = wcs.pixelxy2radec(np.hstack(xx), np.hstack(yy))
    return rr,dd

def get_wcs_radec_bounds(wcs):
    rr,dd = walk_wcs_boundary(wcs)
    r0,r1 = rr.min(), rr.max()
    d0,d1 = dd.min(), dd.max()
    return r0,r1,d0,d1

def get_atlas_tiles(r0,r1,d0,d1, W,H,pixscale):
    '''
    Select Atlas Image tiles touching a desired RA,Dec box.

    pixscale in arcsec/pixel
    '''
    # Read Atlas Image table
    T = fits_table('wise_allsky_4band_p3as_cdd.fits', columns=['coadd_id', 'ra', 'dec'])
    T.row = np.arange(len(T))
    print 'Read', len(T), 'Atlas tiles'

    margin = (max(W,H) / 2.) * (pixscale / 3600.)
    cosdec = np.cos(np.deg2rad(max(abs(d0),abs(d1))))
    mr = margin / cosdec
    
    T.cut((T.ra + mr > r0) *
          (T.ra - mr < r1) *
          (T.dec + margin > d0) *
          (T.dec - margin < d1))
    print 'Cut to', len(T), 'Atlas tiles near RA,Dec box'

    T.coadd_id = np.array([c.replace('_ab41','') for c in T.coadd_id])

    # Some of them don't *actually* touch our RA,Dec box...
    print 'Checking tile RA,Dec bounds...'
    keep = []
    for i in range(len(T)):
        wcs = get_coadd_tile_wcs(T.ra[i], T.dec[i], W, H, pixscale)
        R0,R1,D0,D1 = get_wcs_radec_bounds(wcs)
        if R1 < r0 or R0 > r1 or D1 < d0 or D0 > d1:
            print 'Coadd tile', T.coadd_id[i], 'is outside RA,Dec box'
            continue
        keep.append(i)
    T.cut(np.array(keep))
    print 'Cut to', len(T), 'tiles'

    return T


def get_wise_frames(r0,r1,d0,d1, margin=2.):
    '''
    Returns WISE frames touching the given RA,Dec box plus margin.
    '''
    # Read WISE frame metadata
    WISE = fits_table(os.path.join(wisedir, 'WISE-index-L1b.fits'))
    print 'Read', len(WISE), 'WISE L1b frames'
    WISE.row = np.arange(len(WISE))

    # Coarse cut on RA,Dec box.
    cosdec = np.cos(np.deg2rad(max(abs(d0),abs(d1))))
    WISE.cut((WISE.ra + margin/cosdec > r0) *
             (WISE.ra - margin/cosdec < r1) *
             (WISE.dec + margin > d0) *
             (WISE.dec - margin < d1))
    print 'Cut to', len(WISE), 'WISE frames near RA,Dec box'

    # Join to WISE Single-Frame Metadata Tables
    WISE.qual_frame = np.zeros(len(WISE), np.int16) - 1
    WISE.moon_masked = np.zeros(len(WISE), bool)
    WISE.dtanneal = np.zeros(len(WISE), np.float32)

    WISE.matched = np.zeros(len(WISE), bool)
    
    for nbands in [2,3,4]:
        fn = os.path.join(wisedir, 'WISE-l1b-metadata-%iband.fits' % nbands)
        T = fits_table(fn, columns=['ra', 'dec', 'scan_id', 'frame_num',
                                    'qual_frame', 'moon_masked', 'dtanneal'])
        print 'Read', len(T), 'from', fn
        # Cut with extra large margins
        T.cut((T.ra  + 2.*margin/cosdec > r0) *
              (T.ra  - 2.*margin/cosdec < r1) *
              (T.dec + 2.*margin > d0) *
              (T.dec - 2.*margin < d1))
        print 'Cut to', len(T), 'near RA,Dec box'
        if len(T) == 0:
            continue

        I,J,d = match_radec(WISE.ra, WISE.dec, T.ra, T.dec, 60./3600.)
        print 'Matched', len(I)
        K = np.flatnonzero((WISE.scan_id  [I] == T.scan_id  [J]) *
                           (WISE.frame_num[I] == T.frame_num[J]))
        I = I[K]
        J = J[K]
        print 'Cut to', len(I), 'matching scan/frame'

        for band in [1,2,3,4]:
            K = (WISE.band[I] == band)
            print 'Band', band, ':', sum(K)
            if sum(K) == 0:
                continue
            WISE.qual_frame [I[K]] = T.qual_frame [J[K]].astype(WISE.qual_frame.dtype)
            moon = T.moon_masked[J[K]]
            print 'Moon:', np.unique(moon)
            print 'moon[%i]:' % (band-1), np.unique([m[band-1] for m in moon])
            WISE.moon_masked[I[K]] = np.array([m[band-1] == '1' for m in moon]).astype(WISE.moon_masked.dtype)
            WISE.dtanneal   [I[K]] = T.dtanneal[J[K]].astype(WISE.dtanneal.dtype)
            print 'moon_masked:', np.unique(WISE.moon_masked)
            WISE.matched[I[K]] = True

    print np.sum(WISE.matched), 'of', len(WISE), 'matched to metadata tables'
    assert(np.sum(WISE.matched) == len(WISE))
    WISE.delete_column('matched')
    return WISE

def check_md5s(WISE):

    from astrometry.util.run_command import run_command
    from astrometry.util.file import read_file

    for i in np.lexsort((WISE.band, WISE.frame_num, WISE.scan_id)):
        intfn = get_l1b_file(wisedir, WISE.scan_id[i], WISE.frame_num[i], WISE.band[i])
        uncfn = intfn.replace('-int-', '-unc-')
        if unc_gz:
            uncfn = uncfn + '.gz'
        maskfn = intfn.replace('-int-', '-msk-')
        if mask_gz:
            maskfn = maskfn + '.gz'
        #print 'intfn', intfn
        #print 'uncfn', uncfn
        #print 'maskfn', maskfn

        instr = ''
        for fn in [intfn,uncfn,maskfn]:
            if not os.path.exists(fn):
                print '%s: DOES NOT EXIST' % fn
                continue
            md5 = read_file(fn + '.md5')
            instr += '%s  %s\n' % (md5, fn)
        if len(instr):
            cmd = "echo '%s' | md5sum -c" % instr
            rtn,out,err = run_command(cmd)
            print out, err
            if rtn:
                print 'ERROR: return code', rtn

def one_coadd(ti, band, W, H, pixscale, WISE,
              ps, wishlist, outdir, mp, do_cube, plots2,
              frame0, nframes):
    print 'Coadd tile', ti.coadd_id
    print 'RA,Dec', ti.ra, ti.dec
    print 'Band', band

    version = {}
    rtn,out,err = run_command('svn info')
    assert(rtn == 0)
    lines = out.split('\n')
    lines = [l for l in lines if len(l)]
    for l in lines:
        words = l.split(':', 1)
        words = [w.strip() for w in words]
        version[words[0]] = words[1]
    print 'SVN version info:', version

    tag = 'unwise-%s-w%i' % (ti.coadd_id, band)
    prefix = os.path.join(outdir, tag)
    ofn = prefix + '-img.fits'
    if os.path.exists(ofn):
        print 'Output file exists:', ofn
        return 0

    cowcs = get_coadd_tile_wcs(ti.ra, ti.dec, W, H, pixscale)
    copoly = np.array(zip(*walk_wcs_boundary(cowcs, step=W/2., margin=10)))

    margin = (1.1 # safety margin
              * (np.sqrt(2.) / 2.) # diagonal
              * (max(W,H) + 1016) # WISE FOV, coadd FOV side length
              * pixscale/3600.) # in deg
    t0 = Time()

    # cut
    WISE = WISE[WISE.band == band]
    WISE.cut(degrees_between(ti.ra, ti.dec, WISE.ra, WISE.dec) < margin)
    print 'Found', len(WISE), 'WISE frames in range and in band W%i' % band

    # reorder by dist from center
    #I = np.argsort(degrees_between(ti.ra, ti.dec, WISE.ra, WISE.dec))
    #WISE.cut(I)

    # cut on RA,Dec box too
    r0,d0 = copoly.min(axis=0)
    r1,d1 = copoly.max(axis=0)
    dd = np.sqrt(2.) * (1016./2.) * (pixscale/3600.) * 1.01 # safety
    dr = dd / min([np.cos(np.deg2rad(d)) for d in [d0,d1]])
    WISE.cut((WISE.ra  + dr >= r0) * (WISE.ra  - dr <= r1) *
             (WISE.dec + dd >= d0) * (WISE.dec - dd <= d1))
    print 'cut to', len(WISE), 'in RA,Dec box'

    print 'Qual_frame scores:', np.unique(WISE.qual_frame)
    WISE.cut(WISE.qual_frame > 0)
    print 'Cut out qual_frame = 0;', len(WISE), 'remaining'
    
    print 'Moon_masked:', np.unique(WISE.moon_masked)
    WISE.cut(WISE.moon_masked == False)
    print 'Cut moon_masked:', len(WISE), 'remaining'

    if band in [3,4]:
        WISE.cut(WISE.dtanneal > 2000.)
        print 'Cut out dtanneal <= 2000 seconds:', len(WISE), 'remaining'

    if band == 4:
        ok = np.array([np.logical_or(s < '03752a', s > '03761b')
                       for s in WISE.scan_id])
        WISE.cut(ok)
        print 'Cut out bad scans in W4:', len(WISE), 'remaining'

    if frame0 or nframes:
        i0 = frame0
        if nframes:
            WISE = WISE[frame0:frame0 + nframes]
        else:
            WISE = WISE[frame0:]
        print 'Cut to', len(WISE), 'frames starting from index', frame0
        
    if wishlist:
        for wise in WISE:
            intfn = get_l1b_file(wisedir, wise.scan_id, wise.frame_num, band)
            if not os.path.exists(intfn):
                print 'Need:', intfn
        return 0

    # *inclusive* coordinates of the bounding-box in the coadd of this image
    # (x0,x1,y0,y1)
    WISE.coextent = np.zeros((len(WISE), 4), int)
    # *inclusive* coordinates of the bounding-box in the image overlapping coadd
    WISE.imextent = np.zeros((len(WISE), 4), int)

    failedfiles = []
    res = []
    pixinrange = 0.

    for wi,wise in enumerate(WISE):
        print
        print (wi+1), 'of', len(WISE)
        intfn = get_l1b_file(wisedir, wise.scan_id, wise.frame_num, band)
        print 'intfn', intfn
        try:
            wcs = Sip(intfn)
        except RuntimeError:
            import traceback
            traceback.print_exc()
            failedfiles.append(intfn)
            continue

        h,w = wcs.get_height(), wcs.get_width()
        poly = np.array(zip(*walk_wcs_boundary(wcs, step=2.*w, margin=10)))
        intersects = polygons_intersect(copoly, poly)

        if not intersects:
            print 'Image does not intersect target'
            res.append(None)
            continue
        res.append((intfn, wcs, w, h, poly))

        cpoly = clip_polygon(copoly, poly)
        xy = np.array([cowcs.radec2pixelxy(r,d)[1:] for r,d in cpoly])
        xy -= 1
        x0,y0 = np.floor(xy.min(axis=0)).astype(int)
        x1,y1 = np.ceil (xy.max(axis=0)).astype(int)
        WISE.coextent[wi,:] = [np.clip(x0, 0, W-1),
                               np.clip(x1, 0, W-1),
                               np.clip(y0, 0, H-1),
                               np.clip(y1, 0, H-1)]

        xy = np.array([wcs.radec2pixelxy(r,d)[1:] for r,d in cpoly])
        xy -= 1
        x0,y0 = np.floor(xy.min(axis=0)).astype(int)
        x1,y1 = np.ceil (xy.max(axis=0)).astype(int)
        WISE.imextent[wi,:] = [np.clip(x0, 0, w-1),
                               np.clip(x1, 0, w-1),
                               np.clip(y0, 0, h-1),
                               np.clip(y1, 0, h-1)]

        print 'wi', wi
        print 'row', WISE.row[wi]
        print 'Image extent:', WISE.imextent[wi,:]
        print 'Coadd extent:', WISE.coextent[wi,:]

        e = WISE.coextent[wi,:]
        pixinrange += (1+e[1]-e[0]) * (1+e[3]-e[2])
        print 'Total pixels in coadd space:', pixinrange

    if len(failedfiles):
        print len(failedfiles), 'failed:'
        for f in failedfiles:
            print '  ', f
        return -1

    I = np.flatnonzero(np.array([r is not None for r in res]))
    WISE.cut(I)
    print 'Cut to', len(WISE), 'intersecting target'
    res = [r for r in res if r is not None]
    WISE.intfn = np.array([r[0] for r in res])
    WISE.wcs = np.array([r[1] for r in res])

    t1 = Time()
    print 'Up to coadd_wise:'
    print t1 - t0

    try:
        (coim,coiv,copp,con, coimb,coivb,coppb,conb,masks, cube, cosky
         )= coadd_wise(cowcs, WISE, ps, band, mp, do_cube, plots2=plots2)
    except:
        print 'coadd_wise failed:'
        import traceback
        traceback.print_exc()
        print 'time up to failure:'
        t2 = Time()
        print t2 - t1
        return
    t2 = Time()
    print 'coadd_wise:'
    print t2 - t1

    f,wcsfn = tempfile.mkstemp()
    os.close(f)
    cowcs.write_to(wcsfn)
    hdr = fitsio.read_header(wcsfn)
    os.remove(wcsfn)

    hdr.add_record(dict(name='MAGZP', value=22.5, comment='Magnitude zeropoint (in Vega mag)'))
    hdr.add_record(dict(name='UNW_SKY', value=cosky,
                        comment='Background value subtracted from coadd img'))
    hdr.add_record(dict(name='UNW_VER', value=version['Revision'],
                        comment='unWISE code SVN revision'))
    hdr.add_record(dict(name='UNW_URL', value=version['URL'], comment='SVN URL'))
    hdr.add_record(dict(name='UNW_DATE', value=datetime.datetime.now().isoformat(),
                        comment='unWISE run time'))

    ofn = prefix + '-img.fits'
    fitsio.write(ofn, coim.astype(np.float32), header=hdr, clobber=True)
    ofn = prefix + '-invvar.fits'
    fitsio.write(ofn, coiv.astype(np.float32), header=hdr, clobber=True)
    ofn = prefix + '-ppstd.fits'
    fitsio.write(ofn, copp.astype(np.float32), header=hdr, clobber=True)
    ofn = prefix + '-n.fits'
    fitsio.write(ofn, con.astype(np.int16), header=hdr, clobber=True)

    ofn = prefix + '-img-w.fits'
    fitsio.write(ofn, coimb.astype(np.float32), header=hdr, clobber=True)
    ofn = prefix + '-invvar-w.fits'
    fitsio.write(ofn, coivb.astype(np.float32), header=hdr, clobber=True)
    ofn = prefix + '-ppstd-w.fits'
    fitsio.write(ofn, coppb.astype(np.float32), header=hdr, clobber=True)
    ofn = prefix + '-n-w.fits'
    fitsio.write(ofn, conb.astype(np.int16), header=hdr, clobber=True)

    if do_cube:
        ofn = prefix + '-cube.fits'
        fitsio.write(ofn, cube.astype(np.float32), header=hdr, clobber=True)

    ii = []
    for i,(mm,r) in enumerate(zip(masks, res)):
        if mm is None:
            continue
        ii.append(i)

        if not mm.included:
            continue

        maskdir = os.path.join(outdir, 'masks-' + tag)
        if not os.path.exists(maskdir):
            os.mkdir(maskdir)

        ofn = WISE.intfn[i].replace('-int', '')
        ofn = os.path.join(maskdir, 'unwise-mask-' + ti.coadd_id + '-'
                           + os.path.basename(ofn))
        (nil,wcs,w,h,poly) = r
        fullmask = np.zeros((h,w), mm.omask.dtype)
        x0,x1,y0,y1 = WISE.imextent[i,:]
        fullmask[y0:y1+1, x0:x1+1] = mm.omask
        fitsio.write(ofn, fullmask, clobber=True)

        cmd = 'gzip -f %s' % ofn
        print 'Running:', cmd
        rtn = os.system(cmd)
        print 'Result:', rtn

    WISE.cut(np.array(ii))
    masks = [masks[i] for i in ii]

    WISE.coadd_sky  = np.array([m.sky for m in masks])
    WISE.coadd_dsky = np.array([m.dsky for m in masks])
    WISE.zeropoint  = np.array([m.zp for m in masks])
    WISE.npixoverlap = np.array([m.ncopix for m in masks])
    WISE.npixpatched = np.array([m.npatched for m in masks])
    WISE.npixrchi    = np.array([m.nrchipix for m in masks])
    WISE.included    = np.array([m.included for m in masks]).astype(np.uint8)
    WISE.weight      = np.array([m.w for m in masks]).astype(np.float32)

    WISE.delete_column('wcs')

    ofn = prefix + '-frames.fits'
    WISE.writeto(ofn)


    return 0

def plot_region(r0,r1,d0,d1, ps, T, WISE, wcsfns, W, H, pixscale):
    maxcosdec = np.cos(np.deg2rad(min(abs(d0),abs(d1))))
    plot = Plotstuff(outformat='png', size=(800,800),
                     rdw=((r0+r1)/2., (d0+d1)/2., 1.05*max(d1-d0, (r1-r0)*maxcosdec)))

    for i in range(3):
        if i in [0,2]:
            plot.color = 'verydarkblue'
        else:
            plot.color = 'black'
        plot.plot('fill')
        plot.color = 'white'
        out = plot.outline

        if i == 0:
            if T is None:
                continue
            plot.alpha = 0.5
            for ti in T:
                cowcs = get_coadd_tile_wcs(ti.ra, ti.dec, W, H, pixscale)
                out.wcs = anwcs_new_tan(cowcs)
                out.fill = 1
                plot.plot('outline')
                out.fill = 0
                plot.plot('outline')
        elif i == 1:
            if WISE is None:
                continue
            # cut
            #WISE = WISE[WISE.band == band]
            plot.alpha = (3./256.)
            out.fill = 1
            print 'Plotting', len(WISE), 'exposures'
            wcsparams = []
            fns = []
            for wi,wise in enumerate(WISE):
                if wi % 10 == 0:
                    print '.',
                if wi % 1000 == 0:
                    print wi, 'of', len(WISE)

                if wi and wi % 10000 == 0:
                    fn = ps.getnext()
                    plot.write(fn)
                    print 'Wrote', fn

                    wp = np.array(wcsparams)
                    WW = fits_table()
                    WW.crpix  = wp[:, 0:2]
                    WW.crval  = wp[:, 2:4]
                    WW.cd     = wp[:, 4:8]
                    WW.imagew = wp[:, 8]
                    WW.imageh = wp[:, 9]
                    WW.intfn = np.array(fns)
                    WW.writeto('sequels-wcs.fits')

                intfn = get_l1b_file(wisedir, wise.scan_id, wise.frame_num, wise.band)
                try:
                    wcs = Tan(intfn, 0, 1)
                except:
                    import traceback
                    traceback.print_exc()
                    continue
                out.wcs = anwcs_new_tan(wcs)
                plot.plot('outline')

                wcsparams.append((wcs.crpix[0], wcs.crpix[1], wcs.crval[0], wcs.crval[1],
                                  wcs.cd[0], wcs.cd[1], wcs.cd[2], wcs.cd[3],
                                  wcs.imagew, wcs.imageh))
                fns.append(intfn)

            wp = np.array(wcsparams)
            WW = fits_table()
            WW.crpix  = wp[:, 0:2]
            WW.crval  = wp[:, 2:4]
            WW.cd     = wp[:, 4:8]
            WW.imagew = wp[:, 8]
            WW.imageh = wp[:, 9]
            WW.intfn = np.array(fns)
            WW.writeto('sequels-wcs.fits')

            fn = ps.getnext()
            plot.write(fn)
            print 'Wrote', fn

        elif i == 2:
            if wcsfns is None:
                continue
            plot.alpha = 0.5
            for fn in wcsfns:
                out.set_wcs_file(fn, 0)
                out.fill = 1
                plot.plot('outline')
                out.fill = 0
                plot.plot('outline')


        plot.color = 'gray'
        plot.alpha = 1.
        grid = plot.grid
        grid.ralabeldir = 2
        grid.ralo = 120
        grid.rahi = 200
        grid.declo = 30
        grid.dechi = 60
        plot.plot_grid(5, 5, 20, 10)
        plot.color = 'red'
        plot.apply_settings()
        plot.line_constant_dec(d0, r0, r1)
        plot.stroke()
        plot.line_constant_ra(r1, d0, d1)
        plot.stroke()
        plot.line_constant_dec(d1, r1, r0)
        plot.stroke()
        plot.line_constant_ra(r0, d1, d0)
        plot.stroke()
        fn = ps.getnext()
        plot.write(fn)
        print 'Wrote', fn


def _bounce_one_round2(*A):
    try:
        return _coadd_one_round2(*A)
    except:
        import traceback
        print '_coadd_one_round2 failed:'
        traceback.print_exc()
        raise

def _coadd_one_round2((ri, N, scanid, rr, cow1, cowimg1, cowimgsq1, tinyw, plotfn, ps1)):
    if rr is None:
        return None
    print 'Coadd round 2, image', (ri+1), 'of', N
    t00 = Time()
    mm = Duck()
    mm.npatched = rr.npatched
    mm.ncopix = rr.ncopix
    mm.sky = rr.sky
    mm.zp = rr.zp
    mm.w = rr.w
    mm.included = True

    cox0,cox1,coy0,coy1 = rr.coextent
    coslc = slice(coy0, coy1+1), slice(cox0, cox1+1)
    # Remove this image from the per-pixel std calculation...
    subw  = np.maximum(cow1[coslc] - rr.w, tinyw)
    subco = (cowimg1  [coslc] - (rr.w * rr.rimg   )) / subw
    subsq = (cowimgsq1[coslc] - (rr.w * rr.rimg**2)) / subw
    subpp = np.sqrt(np.maximum(0, subsq - subco**2))

    # like in the WISE Atlas Images, estimate sky difference via
    # median difference in the overlapping area.
    # dsky = median_f((rr.rimg[rr.rmask] - subco[rr.rmask]).astype(np.float32))
    # print 'Sky difference:', dsky
    # DEBUG
    dsky = 0.
    print 'WARNING: setting dsky = 0'


    rchi = ((rr.rimg - dsky - subco) * rr.rmask * (subw > 0) * (subpp > 0) /
            np.maximum(subpp, 1e-6))
    #print 'rchi', rchi.min(), rchi.max()
    assert(np.all(np.isfinite(rchi)))
    badpix = (np.abs(rchi) >= 5.)
    #print 'Number of rchi-bad pixels:', np.count_nonzero(badpix)
    mm.nrchipix = np.count_nonzero(badpix)

    # Bit 1: abs(rchi) >= 5
    badpixmask = badpix.astype(np.uint8)
    # grow by a small margin
    badpix = binary_dilation(badpix)
    # Bit 2: grown
    badpixmask += (2 * badpix)
    # Add rchi-masked pixels to the mask
    rr.rmask2[badpix] = False
    # print 'Applying rchi masks to images...'
    mm.omask = np.zeros((rr.wcs.get_height(), rr.wcs.get_width()),
                        badpixmask.dtype)
    try:
        Yo,Xo,Yi,Xi,nil = resample_with_wcs(rr.wcs, rr.cosubwcs, [], None)
        mm.omask[Yo,Xo] = badpixmask[Yi,Xi]
    except OverlapError:
        import traceback
        print 'WARNING: Caught OverlapError resampling rchi mask'
        print 'rr WCS', rr.wcs
        print 'shape', mm.omask.shape
        print 'cosubwcs:', rr.cosubwcs
        traceback.print_exc(None, sys.stdout)

    if mm.nrchipix > mm.ncopix * 0.01:
        print ('WARNING: dropping exposure %s: n rchi pixels %i / %i' %
               (scanid, mm.nrchipix, mm.ncopix))
                                        
        mm.included = False

    if ps1:
        # save for later
        mm.rchi = rchi
        mm.badpix = badpix
        if mm.included:
            mm.rimg_orig = rr.rimg.copy()
            mm.rmask_orig = rr.rmask.copy()

    if mm.included:
        ok = patch_image(rr.rimg, np.logical_not(badpix),
                         required=(badpix * rr.rmask))
        if not ok:
            print 'patch_image failed'
            return None

        rimg = (rr.rimg - dsky)

        mm.coslc = coslc
        mm.coimgsq = rr.rmask * rr.w * rimg**2
        mm.coimg   = rr.rmask * rr.w * rimg
        mm.cow     = rr.rmask * rr.w
        mm.con     = rr.rmask
        mm.rmask2  = rr.rmask2

    mm.dsky = dsky / rr.zpscale

        
    if plotfn:

        # HACK
        rchihistrange = 6
        rchihistargs = dict(range=(-rchihistrange,rchihistrange), bins=100)
        rchihist = None
        rchihistedges = None

        R,C = 3,3
        plt.clf()
        plt.subplot(R,C,1)
        I = rr.rimg - dsky
        # print 'rimg shape', rr.rimg.shape
        # print 'rmask shape', rr.rmask.shape
        # print 'rmask elements set:', np.sum(rr.rmask)
        # print 'len I[rmask]:', len(I[rr.rmask])
        if len(I[rr.rmask]):
            plo,phi = [np.percentile(I[rr.rmask], p) for p in [25,99]]
            plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                       vmin=plo, vmax=phi)
            plt.xticks([]); plt.yticks([])
            plt.title('rimg')

        plt.subplot(R,C,2)
        I = subco
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.xticks([]); plt.yticks([])
        plt.title('subco')
        plt.subplot(R,C,3)
        I = subpp
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.xticks([]); plt.yticks([])
        plt.title('subpp')
        plt.subplot(R,C,4)
        plt.imshow(rchi, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=-5, vmax=5)
        plt.xticks([]); plt.yticks([])
        plt.title('rchi (%i)' % mm.nrchipix)

        plt.subplot(R,C,5)
        I = rr.img
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.xticks([]); plt.yticks([])
        plt.title('img')

        plt.subplot(R,C,6)
        I = mm.omask
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=0, vmax=3)
        plt.xticks([]); plt.yticks([])
        plt.title('omask')

        plt.subplot(R,C,7)
        I = rr.rimg
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.xticks([]); plt.yticks([])
        plt.title('patched rimg')

        # plt.subplot(R,C,8)
        # I = (coimgb / np.maximum(cowb, tinyw))
        # plo,phi = [np.percentile(I, p) for p in [25,99]]
        # plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
        #            vmin=plo, vmax=phi)
        # plt.xticks([]); plt.yticks([])
        # plt.title('coimgb')

        I = (rchi != 0.)
        n,e = np.histogram(np.clip(rchi[I], -rchihistrange, rchihistrange),
                           **rchihistargs)
        if rchihist is None:
            rchihist, rchihistedges = n,e
        else:
            rchihist += n

        plt.subplot(R,C,9)
        e = rchihistedges
        e = (e[:-1]+e[1:])/2.
        #plt.semilogy(e, np.maximum(0.1, rchihist), 'b-')
        plt.semilogy(e, np.maximum(0.1, n), 'b-')
        plt.axvline(5., color='r')
        plt.xlim(-(rchihistrange+1), rchihistrange+1)
        plt.yticks([])
        plt.title('rchi')

        inc = ''
        if not mm.included:
            inc = '(not incl)'
        plt.suptitle('%s %s' % (scanid, inc))
        plt.savefig(plotfn)

    print 'Coadd round 2, image', (ri+1), 'of', N, ':\n', Time() - t00
    return mm

class coaddacc():
    def __init__(self, H,W, do_cube=False, nims=0):
        self.coimg    = np.zeros((H,W))
        self.coimgsq  = np.zeros((H,W))
        self.cow      = np.zeros((H,W))
        self.con      = np.zeros((H,W), np.int16)
        self.coimgb   = np.zeros((H,W))
        self.coimgsqb = np.zeros((H,W))
        self.cowb     = np.zeros((H,W))
        self.conb     = np.zeros((H,W), np.int16)

        if do_cube:
            self.cube = np.zeros((nims, H, W), np.float32)
            self.cubei = 0
        else:
            self.cube = None
            
    def acc(self, mm, delmm=False):
        if mm is None or not mm.included:
            return
        self.coimgsq [mm.coslc] += mm.coimgsq
        self.coimg   [mm.coslc] += mm.coimg
        self.cow     [mm.coslc] += mm.cow
        self.con     [mm.coslc] += mm.con
        self.coimgsqb[mm.coslc] += mm.rmask2 * mm.coimgsq
        self.coimgb  [mm.coslc] += mm.rmask2 * mm.coimg
        self.cowb    [mm.coslc] += mm.rmask2 * mm.cow
        self.conb    [mm.coslc] += mm.rmask2 * mm.con
        if self.cube is not None:
            self.cube[(self.cubei,) + mm.coslc] = (mm.coimgb).astype(self.cube.dtype)
            self.cubei += 1
        if delmm:
            del mm.coimgsq
            del mm.coimg
            del mm.cow
            del mm.con
            del mm.rmask2
        

def coadd_wise(cowcs, WISE, ps, band, mp, do_cube, plots2=False, table=True):
    L = 3
    W = cowcs.get_width()
    H = cowcs.get_height()
    # For W4, single-image ww is ~ 1e-10
    tinyw = 1e-16

    # DEBUG
    #WISE = WISE[:10]
    # DEBUG -- scan closest to outlier 03833a
    #WISE.hexscan = np.array([int(s, 16) for s in WISE.scan_id])
    #WISE.cut(np.lexsort((WISE.frame_num, np.abs(WISE.hexscan - int('03833a', 16)))))
    #WISE.cut(np.lexsort((WISE.frame_num, WISE.scan_id)))

    (rimgs, coimg1, cow1, coppstd1, cowimgsq1
     )= _coadd_wise_round1(cowcs, WISE, ps, band, table, L, tinyw, mp)
    cowimg1 = coimg1 * cow1

    # Using the difference between the coadd and the resampled
    # individual images ("rchi"), mask additional pixels and redo the
    # coadd.

    assert(len(rimgs) == len(WISE))

    if ps:
        # Plots of round-one per-image results.
        plt.figure(figsize=(4,4))
        plt.subplots_adjust(left=0.01, right=0.99, bottom=0.01, top=0.99)
        ngood = 0
        for i,rr in enumerate(rimgs):
            if ngood >= 5:
                break
            if rr is None:
                continue
            if rr.ncopix < 0.25 * W*H:
                continue
            #print 'rmask', np.sum(rr.rmask), 'vs rmask2', np.sum(rr.rmask2), 'diff', np.sum(rr.rmask)-np.sum(rr.rmask2)
            ngood += 1

            print 'Plotting rr', i

            plt.clf()
            cim = np.zeros((H,W))
            # Make untouched pixels white.
            cim += 1e10
            cox0,cox1,coy0,coy1 = rr.coextent
            slc = slice(coy0,coy1+1), slice(cox0,cox1+1)
            cim[slc][rr.rmask] = rr.rimg[rr.rmask]
            sig1 = 1./np.sqrt(rr.w)
            plt.imshow(cim, interpolation='nearest', origin='lower', cmap='gray',
                       vmin=-1.*sig1, vmax=5.*sig1)
            ps.savefig()

            cmask = np.zeros((H,W), bool)
            cmask[slc] = rr.rmask
            plt.clf()
            # invert
            #plt.imshow(np.logical_not(cmask), interpolation='nearest', origin='lower', cmap='gray',
            plt.imshow(cmask, interpolation='nearest', origin='lower', cmap='gray',
                       vmin=0, vmax=1)
            ps.savefig()

            cmask[slc] = rr.rmask2
            plt.clf()
            #plt.imshow(np.logical_not(cmask), interpolation='nearest', origin='lower', cmap='gray',
            plt.imshow(cmask, interpolation='nearest', origin='lower', cmap='gray',
                       vmin=0, vmax=1)
            ps.savefig()

        sig1 = 1./np.sqrt(np.median(cow1))
        plt.clf()
        plt.imshow(coimg1, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=-1.*sig1, vmax=5.*sig1)
        ps.savefig()

        plt.clf()
        plt.imshow(cow1, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=0, vmax=cow1.max())
        ps.savefig()

        coppstd  = np.sqrt(np.maximum(0, cowimgsq1  / (np.maximum(cow1,  tinyw)) - coimg1 **2))
        mx = np.percentile(coppstd.ravel(), 99)
        plt.clf()
        plt.imshow(coppstd, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=0, vmax=mx)
        ps.savefig()

    # If we're not multiprocessing, do the loop manually to reduce
    # memory usage (we don't need to keep all "rr" inputs and "masks"
    # outputs in memory at once).
    ps1 = (ps is not None)
    delmm = (ps is None)
    if not mp.pool:
        coadd = coaddacc(H, W, do_cube=do_cube, nims=len(rimgs))
        masks = []
        ri = -1
        while len(rimgs):
            ri += 1
            rr = rimgs.pop(0)
            if ps and plots2:
                plotfn = ps.getnext()
            else:
                plotfn = None
            scanid = 'scan %s frame %i band %i' % (WISE.scan_id[ri], WISE.frame_num[ri],
                                                   band)
            mm = _coadd_one_round2(
                (ri, len(WISE), scanid, rr, cow1, cowimg1, cowimgsq1, tinyw, plotfn, ps1))
            coadd.acc(mm, delmm=delmm)
            masks.append(mm)
    else:
        args = []
        N = len(WISE)
        for ri,rr in enumerate(rimgs):
            if ps and plots2:
                plotfn = ps.getnext()
            else:
                plotfn = None
            scanid = 'scan %s frame %i band %i' % (WISE.scan_id[ri], WISE.frame_num[ri],
                                                   band)
            args.append((ri, N, scanid, rr, cow1, cowimg1, cowimgsq1, tinyw, plotfn, ps1))
        #masks = mp.map(_coadd_one_round2, args)
        masks = mp.map(_bounce_one_round2, args)
        del args
        print 'Accumulating second-round coadds...'
        t0 = Time()
        coadd = coaddacc(H, W, do_cube=do_cube, nims=len(rimgs))
        for mm in masks:
            coadd.acc(mm, delmm=delmm)
        print Time()-t0

    if ps:
        ngood = 0
        for i,mm in enumerate(masks):
            if ngood >= 5:
                break
            if mm is None or not mm.included:
                continue
            if sum(mm.badpix) == 0:
                continue
            if mm.ncopix < 0.25 * W*H:
                continue
            ngood += 1

            print 'Plotting mm', i

            cim = np.zeros((H,W))
            cim += 1e6
            cim[mm.coslc][mm.rmask_orig] = mm.rimg_orig[mm.rmask_orig]
            w = np.max(mm.cow)
            sig1 = 1./np.sqrt(w)

            cbadpix = np.zeros((H,W))
            cbadpix[mm.coslc][mm.con] = mm.badpix[mm.con]
            blobs,nblobs = label(cbadpix, np.ones((3,3),int))
            blobcms = center_of_mass(cbadpix, labels=blobs, index=range(nblobs+1))

            plt.clf()
            plt.imshow(cim, interpolation='nearest', origin='lower', cmap='gray',
                       vmin=-1.*sig1, vmax=5.*sig1)
            ax = plt.axis()
            for y,x in blobcms:
                plt.plot(x, y, 'o', mec='r', mew=2, mfc='none', ms=15)
            plt.axis(ax)
            ps.savefig()

            cim[mm.coslc][mm.rmask_orig] = mm.rimg_orig[mm.rmask_orig] - coimg1[mm.rmask_orig]
            plt.clf()
            plt.imshow(cim, interpolation='nearest', origin='lower', cmap='gray',
                       vmin=-3.*sig1, vmax=3.*sig1)
            ps.savefig()

            crchi = np.zeros((H,W))
            crchi[mm.coslc] = mm.rchi
            plt.clf()
            plt.imshow(crchi, interpolation='nearest', origin='lower', cmap='gray',
                       vmin=-5, vmax=5)
            ps.savefig()

            cbadpix[:,:] = 0.5
            cbadpix[mm.coslc][mm.con] = (1 - mm.badpix[mm.con])
            plt.clf()
            plt.imshow(cbadpix, interpolation='nearest', origin='lower', cmap='gray',
                       vmin=0, vmax=1)
            ps.savefig()

            #print 'nblobs', nblobs
            #print 'blobcms', blobcms
            #if nblobs == 1:
            #    blobcms = [blobcms]
                
            # Patched image
            # cim += 1e6
            # w = np.max(mm.cow)
            # cim[mm.coslc][mm.con] = mm.coimg[mm.con] / w
            # sig1 = 1./np.sqrt(w)
            # plt.clf()
            # plt.imshow(cim, interpolation='nearest', origin='lower', cmap='gray',
            #            vmin=-1.*sig1, vmax=5.*sig1)
            # ps.savefig()



    coimg    = coadd.coimg
    coimgsq  = coadd.coimgsq
    cow      = coadd.cow
    con      = coadd.con
    coimgb   = coadd.coimgb
    coimgsqb = coadd.coimgsqb
    cowb     = coadd.cowb
    conb     = coadd.conb
    cube     = coadd.cube

    coimg /= np.maximum(cow, tinyw)
    coinvvar = cow

    coimgb /= np.maximum(cowb, tinyw)
    coinvvarb = cowb

    # per-pixel variance
    coppstd  = np.sqrt(np.maximum(0, coimgsq  / (np.maximum(cow,  tinyw)) - coimg **2))
    coppstdb = np.sqrt(np.maximum(0, coimgsqb / (np.maximum(cowb, tinyw)) - coimgb**2))

    # re-estimate and subtract sky from the coadd.
    # approx median
    med = median_f(coimgb[::4,::4].astype(np.float32))
    sig1 = 1./np.sqrt(median_f(coinvvarb[::4,::4].astype(np.float32)))
    sky = estimate_sky(coimgb, med-2.*sig1, med+1.*sig1, omit=None)
    print 'Estimated coadd sky:', sky
    coimg  -= sky
    coimgb -= sky

    if ps:
        plt.clf()
        I = coimg1
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 1')
        ps.savefig()

        plt.clf()
        I = coppstd1
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd per-pixel std 1')
        ps.savefig()

        plt.clf()
        I = coimg
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 2')
        ps.savefig()

        plt.clf()
        I = coimgb
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 2 (weighted)')
        ps.savefig()

        imlo,imhi = plo,phi

        plt.clf()
        I = coppstd
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 2 per-pixel std')
        ps.savefig()

        plt.clf()
        I = coppstdb
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 2 per-pixel std (weighted)')
        ps.savefig()

        nmax = max(con.max(), conb.max())

        plt.clf()
        I = coppstd
        plo,phi = [np.percentile(I, p) for p in [25,99]]
        plt.imshow(I, interpolation='nearest', origin='lower', cmap='gray',
                   vmin=plo, vmax=phi)
        plt.colorbar()
        plt.title('Coadd round 2 per-pixel std')
        ps.savefig()


    return (coimg,  coinvvar,  coppstd,  con,
            coimgb, coinvvarb, coppstdb, conb,
            masks, cube, sky)


def estimate_sky(img, lo, hi, omit=None, maxdev=0., return_fit=False):
    # Estimate sky level by: compute the histogram within [lo,hi], fit
    # a parabola to the log-counts, return the argmax of that parabola.
    binedges = np.linspace(lo, hi, 25)
    counts,e = np.histogram(img.ravel(), bins=binedges)
    bincenters = binedges[:-1] + (binedges[1]-binedges[0])/2.

    if omit is not None:
        # Omit the bin containing value 'omit'
        okI = np.logical_not((binedges[:-1] < omit) * (omit < binedges[1:]))
        bincenters = bincenters[okI]
        counts = counts[okI]

    b = np.log10(np.maximum(1, counts))

    if maxdev > 0:
        # log-deviation of a bin from the mean of its neighbors --
        de = (b[1:-1] - (b[:-2] + b[2:])/2)
        print 'Max deviation:', np.max(de)
        okI = np.append(np.append([True], (de < maxdev)), [True])
        bincenters = bincenters[okI]
        b = b[okI]

    xscale = 0.5 * (hi - lo)
    x0 = (hi + lo) / 2.
    x = (bincenters - x0) / xscale

    A = np.zeros((len(x), 3))
    A[:,0] = 1.
    A[:,1] = x
    A[:,2] = x**2
    res = np.linalg.lstsq(A, b)
    X = res[0]
    mx = -X[1] / (2. * X[2])
    mx = (mx * xscale) + x0

    if return_fit:
        bfit = X[0] + X[1] * x + X[2] * x**2
        return (x * xscale + x0, b, bfit, mx)

    return mx

def _coadd_one_round1((i, N, wise, table, L, ps, band, cowcs)):
    t00 = Time()
    print
    print 'Coadd round 1, image', (i+1), 'of', N
    intfn = wise.intfn
    uncfn = intfn.replace('-int-', '-unc-')
    if unc_gz:
        uncfn = uncfn + '.gz'
    maskfn = intfn.replace('-int-', '-msk-')
    if mask_gz:
        maskfn = maskfn + '.gz'
    print 'intfn', intfn
    print 'uncfn', uncfn
    print 'maskfn', maskfn

    wcs = wise.wcs
    x0,x1,y0,y1 = wise.imextent
    cox0,cox1,coy0,coy1 = wise.coextent

    coW = int(1 + cox1 - cox0)
    coH = int(1 + coy1 - coy0)

    wcs = wcs.get_subimage(int(x0), int(y0), int(1+x1-x0), int(1+y1-y0))
    # We read the full images for sky-estimation purposes -- really necessary?
    slc = (slice(y0,y1+1), slice(x0,x1+1))
    with fitsio.FITS(intfn) as F:
        fullimg = F[0].read()
        #img = F[0][y0:y1+1, x0:x1+1]
        ihdr = F[0].read_header()
    fullmask = fitsio.FITS(maskfn)[0].read()
    fullunc  = fitsio.FITS(uncfn) [0].read()
    img  = fullimg [slc]
    mask = fullmask[slc]
    unc  = fullunc [slc]
    # mask = fitsio.FITS(maskfn)[0][y0:y1+1, x0:x1+1]
    # unc  = fitsio.FITS(uncfn) [0][y0:y1+1, x0:x1+1]
    #print 'Img:', img.shape, img.dtype
    #print 'Unc:', unc.shape, unc.dtype
    #print 'Mask:', mask.shape, mask.dtype

    zp = ihdr['MAGZP']
    zpscale = 1. / NanoMaggies.zeropointToScale(zp)
    print 'Zeropoint:', zp, '-> scale', zpscale

    if band == 4:
        # In W4, the WISE single-exposure images are binned down
        # 2x2, so we are effectively splitting each pixel into 4
        # sub-pixels.  Spread out the flux.
        zpscale *= 0.25

    # 3-band cryo phase:
    #### 19 pixel is "hard-saturated"; see note [4] above
    #### 23 for W3 only: static-split droop residual present

    maskbits = sum([1<<bit for bit in [0,1,2,3,4,5,6,7, 9, 
                                       10,11,12,13,14,15,16,17,18,
                                       21,26,27,28]])
    goodmask = ((mask & maskbits) == 0)
    goodmask[unc == 0] = False
    goodmask[np.logical_not(np.isfinite(img))] = False
    goodmask[np.logical_not(np.isfinite(unc))] = False

    sig1 = median_f(unc[goodmask])
    print 'sig1:', sig1
    del mask
    del unc

    # our return value (quack):
    rr = Duck()

    # Patch masked pixels so we can interpolate
    rr.npatched = np.count_nonzero(np.logical_not(goodmask))
    print 'Pixels to patch:', rr.npatched
    if rr.npatched > 100000:
        print 'WARNING: too many pixels to patch:', rr.npatched
        return None
    ok = patch_image(img, goodmask.copy())
    if not ok:
        print 'WARNING: Patching failed:'
        print 'Image size:', img.shape
        print 'Number to patch:', rr.npatched
        return None
    assert(np.all(np.isfinite(img)))

    # Estimate sky level
    fullok = ((fullmask & maskbits) == 0)
    fullok[fullunc == 0] = False
    fullok[np.logical_not(np.isfinite(fullimg))] = False
    fullok[np.logical_not(np.isfinite(fullunc))] = False
    # approx median
    med = median_f(fullimg[::4,::4][fullok[::4,::4]].astype(np.float32))
    # add some noise to smooth out "dynacal" artifacts
    fim = fullimg[fullok]
    fim += np.random.normal(scale=sig1, size=fim.shape) 
    omit = None
    if ps:
        vals,counts,fitcounts,sky = estimate_sky(fim, med-2.*sig1, med+1.*sig1, omit=omit, return_fit=True)
        rr.hist = np.histogram(fullimg[fullok], range=(med-2.*sig1, med+2.*sig1), bins=100)
        rr.skyest = sky
        rr.skyfit = (vals, counts, fitcounts)
    else:
        sky = estimate_sky(fim, med-2.*sig1, med+1.*sig1, omit=omit)
    del fim
    del fullunc
    del fullok
    del fullimg
    del fullmask
    
    # Convert to nanomaggies
    img -= sky
    img *= zpscale
    sig1 *= zpscale

    # coadd subimage
    cosubwcs = cowcs.get_subimage(int(cox0), int(coy0), coW, coH)
    try:
        Yo,Xo,Yi,Xi,rims = resample_with_wcs(cosubwcs, wcs, [img], L, table=table)
    except OverlapError:
        print 'No overlap; skipping'
        return None
    rim = rims[0]
    assert(np.all(np.isfinite(rim)))

    print 'Pixels in range:', len(Yo)
    #print 'Added to coadd: range', rim.min(), rim.max(), 'mean', np.mean(rim), 'median', np.median(rim)

    if ps:
        # save for later...
        rr.img = img

    # Scalar!
    rr.w = (1./sig1**2)
    rr.rmask = np.zeros((coH, coW), np.bool)
    rr.rmask[Yo, Xo] = True
    rr.rimg = np.zeros((coH, coW), img.dtype)
    rr.rimg[Yo, Xo] = rim
    rr.rmask2 = np.zeros((coH, coW), np.bool)
    rr.rmask2[Yo, Xo] = goodmask[Yi, Xi]
    rr.wcs = wcs
    rr.sky = sky
    rr.zpscale = zpscale
    rr.zp = zp
    rr.ncopix = len(Yo)
    rr.coextent = wise.coextent
    rr.cosubwcs = cosubwcs

    print Time() - t00

    return rr


def _coadd_wise_round1(cowcs, WISE, ps, band, table, L,
                       tinyw, mp):
    W = cowcs.get_width()
    H = cowcs.get_height()
    coimg  = np.zeros((H,W))
    coimgsq = np.zeros((H,W))
    cow    = np.zeros((H,W))

    args = []
    for wi,wise in enumerate(WISE):
        args.append((wi, len(WISE), wise, table, L, ps, band, cowcs))
    rimgs = mp.map(_coadd_one_round1, args)
    del args

    print 'Accumulating first-round coadds...'
    t0 = Time()
    for rr in rimgs:
        if rr is None:
            continue
        cox0,cox1,coy0,coy1 = rr.coextent
        slc = slice(coy0,coy1+1), slice(cox0,cox1+1)

        # note, rr.w is a scalar.
        coimgsq[slc] += rr.w * (rr.rimg**2)
        coimg  [slc] += rr.w *  rr.rimg
        cow    [slc] += rr.w *  rr.rmask
    print Time()-t0

    print 'Min cow (round 1):', cow.min()

    coimg /= np.maximum(cow, tinyw)
    # Per-pixel std
    coppstd = np.sqrt(np.maximum(0, coimgsq / np.maximum(cow, tinyw) - coimg**2))

    if ps:
        # plt.clf()
        # for rr in rimgs:
        #     if rr is None:
        #         continue
        #     n,e = rr.hist
        #     e = (e[:-1] + e[1:])/2.
        #     plt.plot(e - rr.skyest, n, 'b-', alpha=0.1)
        #     plt.axvline(e[0] - rr.skyest, color='r', alpha=0.1)
        #     plt.axvline(e[-1] - rr.skyest, color='r', alpha=0.1)
        # plt.xlabel('image - sky')
        # ps.savefig()
        # plt.yscale('log')
        # ps.savefig()

        plt.clf()
        for rr in rimgs:
            if rr is None:
                continue
            n,e = rr.hist
            ee = e.repeat(2)[1:-1]
            nn = n.repeat(2)
            plt.plot(ee - rr.skyest, nn, 'b-', alpha=0.1)
        plt.xlabel('image - sky')
        ps.savefig()
        plt.yscale('log')
        ps.savefig()

        plt.clf()
        for rr in rimgs:
            if rr is None:
                continue
            vals, counts, fitcounts = rr.skyfit
            plt.plot(vals - rr.skyest, counts, 'b-', alpha=0.1)
            plt.plot(vals - rr.skyest, fitcounts, 'r-', alpha=0.1)
        plt.xlabel('image - sky')
        plt.title('sky hist vs fit')
        ps.savefig()

        plt.clf()
        o = 0
        for rr in rimgs:
            if rr is None:
                continue
            vals, counts, fitcounts = rr.skyfit
            off = o * 0.01
            o += 1
            plt.plot(vals - rr.skyest, counts + off, 'b.-', alpha=0.1)
            plt.plot(vals - rr.skyest, fitcounts + off, 'r.-', alpha=0.1)
        plt.xlabel('image - sky')
        plt.title('sky hist vs fit')
        ps.savefig()

        plt.clf()
        for rr in rimgs:
            if rr is None:
                continue
            vals, counts, fitcounts = rr.skyfit
            plt.plot(vals - rr.skyest, counts - fitcounts, 'b-', alpha=0.1)
        plt.ylabel('log counts - log fit')
        plt.xlabel('image - sky')
        plt.title('sky hist fit residuals')
        ps.savefig()

        plt.clf()
        for rr in rimgs:
            if rr is None:
                continue
            vals, counts, fitcounts = rr.skyfit
            plt.plot(vals - rr.skyest, counts - fitcounts, 'b.', alpha=0.1)
        plt.ylabel('log counts - log fit')
        plt.xlabel('image - sky')
        plt.title('sky hist fit residuals')
        ps.savefig()

        ha = dict(range=(-8,8), bins=100, log=True, histtype='step')
        plt.clf()
        nn = []
        for rr in rimgs:
            if rr is None:
                continue
            rim = rr.rimg[rr.rmask]
            if len(rim) == 0:
                continue
            #n,b,p = plt.hist(rim, alpha=0.1, **ha)
            #nn.append((n,b))
            n,e = np.histogram(rim, range=ha['range'], bins=ha['bins'])
            lo = 3e-3
            nnn = np.maximum(3e-3, n/float(sum(n)))
            #print 'e', e
            #print 'nnn', nnn
            nn.append((nnn,e))
            plt.semilogy((e[:-1]+e[1:])/2., nnn, 'b-', alpha=0.1)
        plt.xlabel('rimg (-sky)')
        #yl,yh = plt.ylim()
        yl,yh = [np.percentile(np.hstack([n for n,e in nn]), p) for p in [3,97]]
        plt.ylim(yl, yh)
        ps.savefig()

        plt.clf()
        for n,b in nn:
            plt.semilogy((b[:-1] + b[1:])/2., n, 'b.', alpha=0.2)
        plt.xlabel('rimg (-sky)')
        plt.ylim(yl, yh)
        ps.savefig()

        plt.clf()
        n,b,p = plt.hist(coimg.ravel(), **ha)
        plt.xlabel('coimg')
        plt.ylim(max(1, min(n)), max(n)*1.1)
        ps.savefig()

    return rimgs, coimg, cow, coppstd, coimgsq



def trymain():
    try:
        main()
    except:
        import traceback
        traceback.print_exc()

def _bounce_one_coadd(A):
    try:
        return one_coadd(*A)
    except:
        import traceback
        print 'one_coadd failed:'
        traceback.print_exc()

def todo(W, H, pixscale, bands=[1,2,3,4]):
    # Check which tiles still need to be done.
    need = []
    for band in bands:
        fns = []
        for i in range(len(T)):
            tag = 'coadd-%s-w%i' % (T.coadd_id[i], band)
            prefix = os.path.join(opt.outdir, tag)
            ofn = prefix + '-img.fits'
            if os.path.exists(ofn):
                print 'Output file exists:', ofn
                fns.append(ofn)
                continue
            need.append(band*1000 + i)

        if band == bands[0]:
            plot_region(r0,r1,d0,d1, ps, T, None, fns, W, H, pixscale)
        else:
            plot_region(r0,r1,d0,d1, ps, None, None, fns, W, H, pixscale)

    print ' '.join('%i' %i for i in need)

    # write out scripts
    for i in need:
        script = '\n'.join(['#! /bin/bash',
                            ('#PBS -N %s-%i' % (dataset, i)),
                            '#PBS -l cput=1:00:00',
                            '#PBS -l pvmem=4gb',
                            'cd $PBS_O_WORKDIR',
                            ('export PBS_ARRAYID=%i' % i),
                            './wise-coadd.py',
                            ''])
                            
        sfn = 'pbs-%s-%i.sh' % (dataset, i)
        write_file(script, sfn)
        os.system('chmod 755 %s' % sfn)

    # Collapse contiguous ranges
    strings = []
    if len(need):
        start = need.pop(0)
        end = start
        while len(need):
            x = need.pop(0)
            if x == end + 1:
                # extend this run
                end = x
            else:
                # run finished; output and start new one.
                if start == end:
                    strings.append('%i' % start)
                else:
                    strings.append('%i-%i' % (start, end))
                start = end = x
        # done; output
        if start == end:
            strings.append('%i' % start)
        else:
            strings.append('%i-%i' % (start, end))
        print ','.join(strings)
    else:
        print 'Done (party now)'
    

def main():
    import optparse
    from astrometry.util.multiproc import multiproc

    parser = optparse.OptionParser('%prog [options]')
    parser.add_option('--threads', dest='threads', type=int, help='Multiproc',
                      default=None)
    parser.add_option('--todo', dest='todo', action='store_true', default=False,
                      help='Print and plot fields to-do')
    parser.add_option('-w', dest='wishlist', action='store_true', default=False,
                      help='Print needed frames and exit?')
    parser.add_option('--plots', dest='plots', action='store_true', default=False)
    parser.add_option('--plots2', dest='plots2', action='store_true', default=False)
    parser.add_option('--pdf', dest='pdf', action='store_true', default=False)

    parser.add_option('--plot-prefix', dest='plotprefix', default=None)

    parser.add_option('--outdir', '-o', dest='outdir', default='unwise-coadds',
                      help='Output directory: default %default')

    parser.add_option('--size', dest='size', default=2048, type=int,
                      help='Set output image size -- DEBUGGING ONLY!')
    parser.add_option('--pixscale', dest='pixscale', type=float, default=2.75,
                      help='Set coadd pixel scale, default %default arcsec/pixel')
    parser.add_option('--cube', dest='cube', action='store_true', default=False,
                      help='Save & write out image cube')

    parser.add_option('--dataset', dest='dataset', default='sequels',
                      help='Dataset (region of sky) to coadd')

    parser.add_option('--frame0', dest='frame0', default=0, type=int,
                      help='Only use a subset of the frames: starting with frame0')
    parser.add_option('--nframes', dest='nframes', default=0, type=int,
                      help='Only use a subset of the frames: number nframes')

    opt,args = parser.parse_args()
    if opt.threads:
        mp = multiproc(opt.threads)
    else:
        mp = multiproc()

    batch = False
    arr = os.environ.get('PBS_ARRAYID')
    if arr is not None:
        arr = int(arr)
        batch = True

    if len(args) == 0 and arr is None:
        print 'No tile(s) specified'
        parser.print_help()
        sys.exit(-1)

    Time.add_measurement(MemMeas)

    dataset = opt.dataset

    if dataset == 'sequels':
        # SEQUELS
        r0,r1 = 120.0, 210.0
        d0,d1 =  45.0,  60.0

    elif dataset == 'm31':
        r0,r1 =  9.0, 12.5
        d0,d1 = 40.5, 42.5
        
    elif dataset == 'npole':
        # North ecliptic pole
        # (270.0, 66.56)
        r0,r1 = 265.0, 275.0
        d0,d1 =  64.6,  68.6
    else:
        assert(False)

    W = H = opt.size

    fn = '%s-atlas.fits' % dataset
    if os.path.exists(fn):
        print 'Reading', fn
        T = fits_table(fn)
    else:
        T = get_atlas_tiles(r0,r1,d0,d1, W,H, opt.pixscale)
        T.writeto(fn)

    fn = '%s-frames.fits' % dataset
    if os.path.exists(fn):
        print 'Reading', fn
        WISE = fits_table(fn)
    else:
        WISE = get_wise_frames(r0,r1,d0,d1)
        # bool -> uint8 to avoid confusing fitsio
        WISE.moon_masked = WISE.moon_masked.astype(np.uint8)
        WISE.writeto(fn)
    WISE.moon_masked = (WISE.moon_masked != 0)

    #WISE.cut(np.logical_or(WISE.band == 1, WISE.band == 2))
    #check_md5s(WISE)

    if opt.plotprefix is None:
        opt.plotprefix = dataset
    ps = PlotSequence(opt.plotprefix, format='%03i')
    if opt.pdf:
        ps.suffixes = ['png','pdf']

    if opt.todo:
        todo(W, H, opt.pixscale)
        sys.exit(0)

    if not opt.plots:
        ps = None

    if not os.path.exists(opt.outdir):
        print 'Creating output directory', opt.outdir
        os.makedirs(opt.outdir)

    if not len(args):
        args.append(arr)

    for a in args:
        tileid = int(a)
        band = tileid / 1000
        tileid = tileid % 1000
        assert(tileid < len(T))
        print 'Doing coadd tile', T.coadd_id[tileid], 'band', band
        t0 = Time()
        one_coadd(T[tileid], band, W, H, opt.pixscale, WISE, ps,
                  opt.wishlist, opt.outdir, mp,
                  opt.cube, opt.plots2, opt.frame0, opt.nframes)
        print 'Tile', T.coadd_id[tileid], 'band', band, 'took:', Time()-t0

if __name__ == '__main__':
    main()
    
