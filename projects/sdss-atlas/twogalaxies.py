# -*- mode: python; indent-tabs-mode: nil -*-
# (this tells emacs to indent with spaces)
import matplotlib
matplotlib.use('Agg')

import os
import logging
import urllib2
import tempfile
import numpy as np
import pylab as plt
import pyfits
import resource
import gc

from astrometry.util.file import *
from astrometry.util.multiproc import multiproc

from tractor import *
from tractor import sdss as st
from tractor.saveImg import *
from tractor import galaxy as sg
from tractor import basics as ba
from tractor.overview import fieldPlot
from tractor.tychodata import tychoMatch
from tractor.rc3 import getName
from tractor.cache import *
from astrometry.sdss.fields import *

from halflight import halflight
from addtodb import add_to_table
import optparse
import sys

def plotarea(ra, dec, radius, name, prefix, tims=None, rds=[]):
    from astrometry.util.util import Tan
    W,H = 512,512
    scale = (radius * 60. * 4) / float(W)
    print 'SDSS jpeg scale', scale
    imgfn = 'sdss-mosaic-%s.png' % prefix
    if not os.path.exists(imgfn):
        url = (('http://skyservice.pha.jhu.edu/DR9/ImgCutout/getjpeg.aspx?' +
                'ra=%f&dec=%f&scale=%f&width=%i&height=%i') %
               (ra, dec, scale, W, H))
        f = urllib2.urlopen(url)
        of,tmpfn = tempfile.mkstemp(suffix='.jpg')
        os.close(of)
        of = open(tmpfn, 'wb')
        of.write(f.read())
        of.close()
        cmd = 'jpegtopnm %s | pnmtopng > %s' % (tmpfn, imgfn)
        os.system(cmd)
    # Create WCS header for it
    cd = scale / 3600.
    args = (ra, dec, W/2. + 0.5, H/2. + 0.5, -cd, 0., 0., -cd, W, H)
    wcs = Tan(*[float(x) for x in args])

    plt.clf()
    I = plt.imread(imgfn)
    plt.imshow(I, interpolation='nearest', origin='lower')
    temp,x,y = wcs.radec2pixelxy(ra, dec)
    R = radius * 60. / scale
    ax = plt.axis()
    plt.gca().add_artist(matplotlib.patches.Circle(xy=(x,y), radius=R, color='g',
                                                   lw=3, alpha=0.5, fc='none'))
    if tims is not None:
        print 'Plotting outlines of', len(tims), 'images'
        for tim in tims:
            H,W = tim.shape
            twcs = tim.getWcs()
            px,py = [],[]
            for x,y in [(1,1),(W,1),(W,H),(1,H),(1,1)]:
                rd = twcs.pixelToPosition(x,y)
                temp,xx,yy = wcs.radec2pixelxy(rd.ra, rd.dec)
                print 'x,y', x, y
                x1,y1 = twcs.positionToPixel(rd)
                print '  x1,y1', x1,y1
                print '  r,d', rd.ra, rd.dec,
                print '  xx,yy', xx, yy
                px.append(xx)
                py.append(yy)
            plt.plot(px, py, 'g-', lw=3, alpha=0.5)

            # plot full-frame image outline too
            # px,py = [],[]
            # W,H = 2048,1489
            # for x,y in [(1,1),(W,1),(W,H),(1,H),(1,1)]:
            #     r,d = twcs.pixelToRaDec(x,y)
            #     xx,yy = wcs.radec2pixelxy(r,d)
            #     px.append(xx)
            #     py.append(yy)
            # plt.plot(px, py, 'g-', lw=1, alpha=1.)

    if rds is not None:
        px,py = [],[]
        for ra,dec in rds:
            print 'ra,dec', ra,dec
            temp,xx,yy = wcs.radec2pixelxy(ra, dec)
            px.append(xx)
            py.append(yy)
        plt.plot(px, py, 'go')

    plt.axis(ax)
    fn = '%s.png' % prefix
    plt.savefig(fn)
    print 'saved', fn

def get_ims_and_srcs((r,c,f,rr,dd, bands, ra, dec, roipix, imkw, getim, getsrc)):
    tims = []
    roi = None
    for band in bands:
        tim,tinf = getim(r, c, f, band, roiradecsize=(ra,dec,roipix), **imkw)
        if tim is None:
            print "Zero roi"
            return None,None
        if roi is None:
            roi = tinf['roi']
        tims.append(tim)
    s = getsrc(r, c, f, roi=roi, bands=bands)
    return (tims,s)


def twogalaxies(name1,ra1,dec1,name2,ra2,dec2):
    name = "%s %s" % (name1,name2)
    ra = float(ra1)
    dec = float(dec1)
    ra2 = float(ra2)
    dec2 = float(dec2)
    remradius = 6.
    fieldradius = 6.
    threads = None
    itune1=5
    itune2=5
    ntune=0
    nocache=True
    #Radius should be in arcminutes
    if threads:
        mp = multiproc(nthreads=threads)
    else:
        mp = multiproc()

    IRLS_scale = 25.
    dr9 = True
    dr8 = False
    noarcsinh = False
    print name

    prefix = '%s' % (name.replace(' ', '_'))
    prefix1 = '%s' % (name1.replace(' ', '_'))
    prefix2 = '%s' % (name2.replace(' ', '_'))
    print 'Removal Radius', remradius
    print 'Field Radius', fieldradius
    print 'RA,Dec', ra, dec

    print os.getcwd()

    rcfs = radec_to_sdss_rcf(ra,dec,radius=math.hypot(fieldradius,13./2.),tablefn="dr9fields.fits")
    print rcfs
    assert(len(rcfs)>0)
    assert(len(rcfs)<15)

    sras, sdecs, smags = tychoMatch(ra,dec,(fieldradius*1.5)/60.)

    for sra,sdec,smag in zip(sras,sdecs,smags):
        print sra,sdec,smag

    imkw = dict(psf='dg')
    if dr9:
        getim = st.get_tractor_image_dr9
        getsrc = st.get_tractor_sources_dr9
        imkw.update(zrange=[-3,100])
    elif dr8:
        getim = st.get_tractor_image_dr8
        getsrc = st.get_tractor_sources_dr8
        imkw.update(zrange=[-3,100])
    else:
        getim = st.get_tractor_image
        getsrc = st.get_tractor_sources_dr8
        imkw.update(useMags=True)

    bands=['u','g','r','i','z']
    bandname = 'r'
    flipBands = ['r']
    print rcfs

    imsrcs = mp.map(get_ims_and_srcs, [(rcf + (bands, ra, dec, fieldradius*60./0.396, imkw, getim, getsrc))
                                       for rcf in rcfs])
    timgs = []
    sources = []
    allsources = []
    for ims,s in imsrcs:
        if ims is None:
            continue
        if s is None:
            continue
        timgs.extend(ims)
        allsources.extend(s)
        sources.append(s)

    #rds = [rcf[3:5] for rcf in rcfs]
    plotarea(ra, dec, fieldradius, name, prefix, timgs) #, rds)
    lvl = logging.DEBUG
    logging.basicConfig(level=lvl,format='%(message)s',stream=sys.stdout)
    tractor = st.Tractor(timgs, allsources, mp=mp)

    sa = dict(debug=True, plotAll=False,plotBands=False)

    if noarcsinh:
        sa.update(nlscale=0)
    elif dr8 or dr9:
        sa.update(chilo=-8.,chihi=8.)

    if nocache:
        tractor.cache = NullCache()
        sg.disable_galaxy_cache()

    zr = timgs[0].zr
    print "zr is: ",zr

    print bands

    print "Number of images: ", len(timgs)
#    for timg,band in zip(timgs,bands):
#        data = timg.getImage()/np.sqrt(timg.getInvvar())
#        plt.hist(data,bins=100)
#        plt.savefig('hist-%s.png' % (band))

    saveAll('initial-'+prefix, tractor,**sa)
    #plotInvvar('initial-'+prefix,tractor)

    

    for sra,sdec,smag in zip(sras,sdecs,smags):
        print sra,sdec,smag

        for img in tractor.getImages():
            wcs = img.getWcs()
            starx,stary = wcs.positionToPixel(RaDecPos(sra,sdec))
            starr=25*(2**(max(11-smag,0.)))
            if starx+starr<0. or starx-starr>img.getWidth() or stary+starr <0. or stary-starr>img.getHeight():
                continue
            X,Y = np.meshgrid(np.arange(img.getWidth()), np.arange(img.getHeight()))
            R2 = (X - starx)**2 + (Y - stary)**2
            img.getStarMask()[R2 < starr**2] = 0

    for timgs,sources in imsrcs:
        timg = timgs[0]
        wcs = timg.getWcs()
        xtr,ytr = wcs.positionToPixel(RaDecPos(ra,dec))
    
        print xtr,ytr

        xt = xtr 
        yt = ytr
        r = ((remradius*60.))/.396 #radius in pixels
        for src in sources:
            xs,ys = wcs.positionToPixel(src.getPosition(),src)
            if (xs-xt)**2+(ys-yt)**2 <= r**2:
                #print "Removed:", src
                #print xs,ys
                tractor.removeSource(src)

    #saveAll('removed-'+prefix, tractor,**sa)
    newShape = sg.GalaxyShape((remradius*60.)/10.,1.,0.)
    newBright = ba.Mags(r=15.0,g=15.0,u=15.0,z=15.0,i=15.0,order=['u','g','r','i','z'])
    EG = st.ExpGalaxy(RaDecPos(ra,dec),newBright,newShape)
    newShape2 = sg.GalaxyShape((remradius*60.)/10.,1.,0.)
    newBright2 = ba.Mags(r=15.0,g=15.0,u=15.0,z=15.0,i=15.0,order=['u','g','r','i','z'])
    EG2 = st.ExpGalaxy(RaDecPos(ra2,dec2),newBright2,newShape2)
    print EG
    print EG2
    tractor.addSource(EG)
    tractor.addSource(EG2)


    saveAll('added-'+prefix,tractor,**sa)


    #print 'Tractor has', tractor.getParamNames()

    for im in tractor.images:
        im.freezeAllParams()
        im.thawParam('sky')
    tractor.catalog.freezeAllBut(EG)
    tractor.catalog.thawParams(EG2)

    #print 'Tractor has', tractor.getParamNames()
    #print 'values', tractor.getParams()

    for i in range(itune1):
        tractor.optimize()
        tractor.changeInvvar(IRLS_scale)
        saveAll('itune1-%d-' % (i+1)+prefix,tractor,**sa)
        tractor.clearCache()
        sg.get_galaxy_cache().clear()
        gc.collect()
        print resource.getpagesize()
        print resource.getrusage(resource.RUSAGE_SELF)[2]
        

    CGPos = EG.getPosition()
    CGShape1 = EG.getShape().copy()
    CGShape2 = EG.getShape().copy()
    EGBright = EG.getBrightness()

    CGu = EGBright[0] + 0.75
    CGg = EGBright[1] + 0.75
    CGr = EGBright[2] + 0.75
    CGi = EGBright[3] + 0.75
    CGz = EGBright[4] + 0.75
    CGBright1 = ba.Mags(r=CGr,g=CGg,u=CGu,z=CGz,i=CGi,order=['u','g','r','i','z'])
    CGBright2 = ba.Mags(r=CGr,g=CGg,u=CGu,z=CGz,i=CGi,order=['u','g','r','i','z'])
    print EGBright
    print CGBright1

    CG = st.CompositeGalaxy(CGPos,CGBright1,CGShape1,CGBright2,CGShape2)

    CG2Pos = EG2.getPosition()
    CG2Shape1 = EG2.getShape().copy()
    CG2Shape2 = EG2.getShape().copy()
    EG2Bright = EG2.getBrightness()

    CG2u = EG2Bright[0] + 0.75
    CG2g = EG2Bright[1] + 0.75
    CG2r = EG2Bright[2] + 0.75
    CG2i = EG2Bright[3] + 0.75
    CG2z = EG2Bright[4] + 0.75
    CG2Bright1 = ba.Mags(r=CG2r,g=CG2g,u=CG2u,z=CG2z,i=CG2i,order=['u','g','r','i','z'])
    CG2Bright2 = ba.Mags(r=CG2r,g=CG2g,u=CG2u,z=CG2z,i=CG2i,order=['u','g','r','i','z'])
    CG2 = st.CompositeGalaxy(CG2Pos,CG2Bright1,CG2Shape1,CG2Bright2,CG2Shape2)

    tractor.removeSource(EG)
    tractor.removeSource(EG2)
    tractor.addSource(CG)
    tractor.addSource(CG2)

    tractor.catalog.freezeAllBut(CG)
    tractor.catalog.thawParams(CG2)
    print resource.getpagesize()
    print resource.getrusage(resource.RUSAGE_SELF)[2]


    for i in range(itune2):
        tractor.optimize()
        tractor.changeInvvar(IRLS_scale)
        saveAll('itune2-%d-' % (i+1)+prefix,tractor,**sa)
        tractor.clearCache()
        sg.get_galaxy_cache().clear()
        print resource.getpagesize()
        print resource.getrusage(resource.RUSAGE_SELF)[2]



    tractor.catalog.thawAllParams()
    for i in range(ntune):
        tractor.optimize()
        tractor.changeInvvar(IRLS_scale)
        saveAll('ntune-%d-' % (i+1)+prefix,tractor,**sa)
    #plotInvvar('final-'+prefix,tractor)
    sa.update(plotBands=True)
    saveAll('allBands-' + prefix,tractor,**sa)

    print CG
    print CG.getPosition()
    print CGBright1
    print CGBright2
    print CGShape1
    print CGShape2
    print CGBright1+CGBright2
    print CG.getBrightness()

    pfn = '%s.pickle' % prefix
    pickle_to_file([CG,CG2],pfn)

    makeflipbook(prefix,len(tractor.getImages()),itune1,itune2,ntune)

    pickle_to_file(CG,"%s.pickle" % prefix1)
    pickle_to_file(CG2,"%s.pickle" % prefix2)
    os.system('cp %s.pickle RC3_Output' % prefix1)
    os.system('cp %s.pickle RC3_Output' % prefix2)

#    halflight('%s' % prefix1)
#    halflight('%s' % prefix2)
#    add_to_table('%s' % prefix1)
#    add_to_table('%s' % prefix2)



def makeflipbook(prefix,numImg,itune1=0,itune2=0,ntune=0):
    # Create a tex flip-book of the plots

    def allImages(title,imgpre,allBands=False):
        page = r'''
        \begin{frame}{%s}
        \plot{data-%s}
        \plot{model-%s} \\
        \plot{diff-%s}
        \plot{chi-%s} \\
        \end{frame}'''
        temp = ''
        for j in range(numImg):
            if j % 5 == 2 or allBands:
                temp+= page % ((title+', %d' % (j),) + (imgpre+'-%d' % (j),)*4)
        return temp

    tex = r'''
    \documentclass[compress]{beamer}
    \usepackage{helvet}
    \newcommand{\plot}[1]{\includegraphics[width=0.5\textwidth]{#1}}
    \begin{document}
    '''
    
    tex+=allImages('Initial Model','initial-'+prefix)
    #tex+=allImages('Removed','removed-'+prefix)
    tex+=allImages('Added','added-'+prefix)
    for i in range(itune1):
        tex+=allImages('Galaxy tuning, step %d' % (i+1),'itune1-%d-' %(i+1)+prefix)
    for i in range(itune2):
        tex+=allImages('Galaxy tuning (w/ Composite), step %d' % (i+1),'itune2-%d-' %(i+1)+prefix)
    for i in range(ntune):
        tex+=allImages('All tuning, step %d' % (i+1),'ntune-%d-' % (i+1)+prefix)

    tex+=allImages('All Bands','allBands-'+prefix,True)
    
    tex += r'\end{document}' + '\n'
    fn = 'flip-' + prefix + '.tex'
    print 'Writing', fn
    open(fn, 'wb').write(tex)
    os.system("pdflatex '%s'" % fn)



if __name__ == '__main__':
    # To profile the code, you can do:
    #import cProfile
    #import sys
    #from datetime import tzinfo, timedelta, datetime
    #cProfile.run('main()','prof-%s.dat' % (datetime.now().isoformat()))
    #sys.exit(0)
    main()
    
