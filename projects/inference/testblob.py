import matplotlib
matplotlib.use('Agg')
import pylab as plt
import numpy as np

import fitsio

from astrometry.util.fits import *
from astrometry.util.util import *
from astrometry.util.plotutils import *
from astrometry.sdss import *

from tractor import *
from tractor.sdss import *

if __name__ == '__main__':

    sdss = DR9()

    run,camcol,field = 1463,4,55
    ra,dec = 270.0, 0.003
    radius = 0.003
    bands = 'ugriz'

    stamp_pattern = 'stamp-%s.fits'
    catfn = 'cat.fits'

    plots = False
    
    # Retrieve SDSS catalog sources in the field
    srcband = 'r'
    
    srcs = get_tractor_sources_dr9(run, camcol, field, bandname=srcband,
                                   sdss=sdss,
                                   radecrad=(ra, dec, radius*np.sqrt(2.)),
                                   nanomaggies=True)
    print 'Got sources:'
    for src in srcs:
        print '  ', src

    fn = sdss.retrieve('photoField', run, camcol, field)
    print 'Retrieved', fn
    F = fits_table(fn)
    F.cut((F.run == run) * (F.camcol == camcol) * (F.field == field))
    print len(F), 'fields'
    assert(len(F) == 1)
    F = F[0]

    # Retrieve SDSS images
    tims = []
    tinfs = []
    pixscale = 0.396/3600.
    for band in bands:
        pixradius = radius / pixscale
        tim,tinfo = get_tractor_image_dr9(run, camcol, field, band, sdss=sdss,
                                          roiradecsize=(ra, dec, pixradius),
                                          nanomaggies=True)
        print 'Got tim:', tim
        frame = sdss.readFrame(run, camcol, field, band)

        x,y = tim.getWcs().positionToPixel(RaDecPos(ra, dec))
        x,y = int(x), int(y)
        # Grab calibration information also
        tim.sdss_calib = np.median(frame.getCalibVec())
        tim.sdss_sky = frame.getSkyAt(x,y)
        iband = band_index(band)
        tim.sdss_gain = F.gain[iband]
        tim.sdss_darkvar = F.dark_variance[iband]
        
        tims.append(tim)
        tinfs.append(tinfo)
        if band == 'r':
            # Cut sources to img bbox
            keep = []
            h,w = tim.shape
            for i,src in enumerate(srcs):
                x,y = tim.getWcs().positionToPixel(src.getPosition())
                if x < 0 or y < 0 or x >= w or y >= h:
                    continue
                keep.append(i)
            srcs = Catalog(*[srcs[i] for i in keep])


    print 'Cut sources:'
    for src in srcs:
        print '  ', src

    # Write out the sources
    T = fits_table()
    T.ra  = [src.getPosition().ra  for src in srcs]
    T.dec = [src.getPosition().dec for src in srcs]
    for band in bands:
        T.set('psfflux_%s' % band,
              [src.getBrightness().getBand(band) for src in srcs])
    T.writeto(catfn)
    

    # Write out the images
    for band,tim,tinfo in zip(bands, tims, tinfs):
        roi = tinfo['roi']
        x0,x1,y0,y1 = roi
        
        if plots:
            plt.clf()
            img = tim.getImage()
            mn,mx = [np.percentile(img,p) for p in [25,99]]
            dimshow(img, vmin=mn, vmax=mx)
            xx,yy = [],[]
            for src in srcs:
                x,y = tim.getWcs().positionToPixel(src.getPosition())
                xx.append(x)
                yy.append(y)
            ax = plt.axis()
            plt.plot(xx, yy, 'r+')
            plt.axis(ax)
            plt.savefig('tim-%s.png' % band)

        cd = tim.getWcs().cdAtPixel((x0+x1)/2., (y0+y1)/2.)
        print 'CD at center:', cd
        crpix1,crpix2 = tim.getWcs().positionToPixel(RaDecPos(ra, dec))
        crpix1 += 1
        crpix2 += 1

        wcs = Tan(ra, dec, crpix1, crpix2, cd[0,0],cd[0,1],cd[1,0],cd[1,1],w,h)
        twcs = ConstantFitsWcs(wcs)
        
        if plots:
            xx,yy = [],[]
            for src in srcs:
                x,y = twcs.positionToPixel(src.getPosition())
                xx.append(x)
                yy.append(y)
            ax = plt.axis()
            plt.plot(xx, yy, 'go', mec='g', mfc='none')
            plt.axis(ax)
            plt.savefig('tim-%s.png' % band)

            tractor = Tractor([tim], srcs)
            mod = tractor.getModelImage(0)

            plt.clf()
            dimshow(mod, vmin=mn, vmax=mx)
            plt.savefig('mod-%s.png' % band)
        

        hdr = fitsio.FITSHDR()
        wcs.add_to_header(hdr)
        hdr.add_record(dict(name='X0', value=x0,
                            comment='X pixel offset in full SDSS image'))
        hdr.add_record(dict(name='Y0', value=y0,
                            comment='Y pixel offset in full SDSS image'))
        hdr.add_record(dict(name='RUN', value=run, comment='SDSS run'))
        hdr.add_record(dict(name='CAMCOL', value=camcol, comment='SDSS camcol'))
        hdr.add_record(dict(name='FIELD', value=field, comment='SDSS field'))
        hdr.add_record(dict(name='BAND', value=band, comment='SDSS band'))

        # Copy from input "frame" header
        orighdr = tinfo['hdr']
        for key in ['NMGY']:
            hdr.add_record(dict(name=key, value=orighdr[key],
                                comment=orighdr.get_comment(key)))

        hdr.add_record(dict(name='CALIB', value=tim.sdss_calib,
                            comment='Mean "calibvec" value for this image'))
        hdr.add_record(dict(name='SKY', value=tim.sdss_sky,
                            comment='SDSS sky estimate at image center'))
        hdr.add_record(dict(name='GAIN', value=tim.sdss_gain,
                            comment='SDSS gain'))
        hdr.add_record(dict(name='DARKVAR', value=tim.sdss_darkvar,
                            comment='SDSS dark variance'))
            
        tim.getPsf().toFitsHeader(hdr, 'PSF_')
            
        fn = stamp_pattern % band
        fitsio.write(fn, tim.getImage(), clobber=True,
                     header=hdr)
        fitsio.write(fn, tim.getInvvar())
