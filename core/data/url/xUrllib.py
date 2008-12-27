'''
xUrllib.py

Copyright 2006 Andres Riancho

This file is part of w3af, w3af.sourceforge.net .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

'''

import urlOpenerSettings
import core.controllers.outputManager as om
import core.data.url.timeAnalysis as timeAnalysis
from core.controllers.w3afException import *
from core.controllers.threads.threadManager import threadManager as tm
from core.data.parsers.urlParser import *
from core.data.constants.httpConstants import *

import core.data.parsers.urlParser as urlParser
from core.data.parsers.httpRequestParser import httpRequestParser

from core.data.request.frFactory import createFuzzableRequestRaw

from core.data.url.httpResponse import httpResponse as httpResponse
from core.data.url.handlers.localCache import CachedResponse
from core.controllers.misc.lru import LRU

# For subclassing requests and other things
import urllib2
import urllib

import time
import os
from core.controllers.misc.homeDir import get_home_dir
from core.controllers.misc.memoryUsage import dumpMemoryUsage

# for better debugging of handlers
import traceback

import core.data.kb.config as cf

class sizeExceeded( Exception ):
    pass
    
class xUrllib:
    '''
    This is a urllib2 wrapper.
    
    @author: Andres Riancho ( andres.riancho@gmail.com )
    '''
    
    def __init__(self):
        self.settings = urlOpenerSettings.urlOpenerSettings()
        self._opener = None
        self._cacheOpener = None
        self._timeAnalysis = None
        self._memoryUsageCounter = 0
        
        # For error handling
        self._lastRequestFailed = False
        self._consecutiveErrorCount = 0
        self._errorCount = {}
        
        self._dnsCache()
        self._tm = tm()
        self._sizeLRU = LRU(200)
        
        # User configured options (in an indirect way)
        self._grepPlugins = []
        self._evasionPlugins = []
        self._paused = False
        self._mustStop = False
        self._ignore_errors_conf = False
    
    def pause(self,  pauseYesNo):
        '''
        When the core wants to pause a scan, it calls this method, in order to freeze all actions
        @parameter pauseYesNo: True if I want to pause the scan; False to un-pause it.
        '''
        self._paused = pauseYesNo
        
    def stop(self):
        '''
        Called when the user wants to finish a scan.
        '''
        self._mustStop = True
    
    def _callBeforeSend(self):
        '''
        This is a method that is called before every request is sent. I'm using it as
        a hook implement:
            - The pause/stop feature
            - Memory debugging features
        '''
        self._sleepIfPausedDieIfStopped()
        
        self._memoryUsageCounter += 1
        if self._memoryUsageCounter == 2000:
            dumpMemoryUsage()
            self._memoryUsageCounter = 0
    
    def _sleepIfPausedDieIfStopped( self ):
        '''
        This method sleeps until self._paused is False.
        '''
        while self._paused:
            time.sleep(0.5)
            
            # The user can pause and then STOP
            if self._mustStop:
                self._mustStop = False
                self._paused = False
                # This raises a keyboard interrupt in the middle of the discovery process.
                # It has almost the same effect that a ctrl+c by the user if in consoleUi
                raise KeyboardInterrupt
        
        # The user can simply STOP the scan
        if self._mustStop:
            self._mustStop = False
            self._paused = False
            # This raises a keyboard interrupt in the middle of the discovery process.
            # It has almost the same effect that a ctrl+c by the user if in consoleUi
            # TODO: THIS SUCKS
            raise KeyboardInterrupt
    
    def end( self ):
        '''
        This method is called when the xUrllib is not going to be used anymore.
        '''
        try:
            cacheLocation = get_home_dir() + os.path.sep + 'urllib2cache' + os.path.sep + str(os.getpid())
            if os.path.exists(cacheLocation):
                for f in os.listdir(cacheLocation):
                    os.unlink( cacheLocation + os.path.sep + f)
                os.rmdir(cacheLocation)
        except Exception, e:
            om.out.debug('Error while cleaning urllib2 cache, exception: ' + str(e) )
        else:
            om.out.debug('Cleared urllib2 local cache.')
    
    def _dnsCache( self ):
        '''
        DNS cache trick
        This will speed up all the test ! Before this dns cache voodoo magic every request
        to the http server needed a dns query, this is slow on some networks so I added
        this feature.
        
        This method was taken from:
        # $Id: download.py,v 1.30 2004/05/13 09:55:30 torh Exp $
        That is part of :
        swup-0.0.20040519/
        
        Developed by:
        #  Copyright 2001 - 2003 Trustix AS - <http://www.trustix.com>
        #  Copyright 2003 - 2004 Tor Hveem - <tor@bash.no>
        #  Copyright 2004 Omar Kilani for tinysofa - <http://www.tinysofa.org>
        '''
        om.out.debug('Enabling _dnsCache()')
        import socket
        if not hasattr( socket, 'already_configured' ):
            socket._getaddrinfo = socket.getaddrinfo
        
        _dns_cache = LRU(200)
        def _caching_getaddrinfo(*args, **kwargs):
            try:
                query = (args)
                res = _dns_cache[query]
                #This was too noisy and not so usefull
                om.out.debug('Cached DNS response for domain: ' + query[0] )
                return res
            except KeyError:
                res = socket._getaddrinfo(*args, **kwargs)
                _dns_cache[args] = res
                om.out.debug('DNS response from DNS server for domain: ' + query[0] )
                return res
        
        if not hasattr( socket, 'already_configured' ):      
            socket.getaddrinfo = _caching_getaddrinfo
            socket.already_configured = True
    
    def _init( self ):
        if self.settings.needUpdate or \
        self._opener == None or self._cacheOpener == None:
        
            self.settings.needUpdate = False
            self.settings.buildOpeners()
            self._opener = self.settings.getCustomUrlopen()
            self._cacheOpener = self.settings.getCachedUrlopen()
            self._timeAnalysis = timeAnalysis.timeAnalysis()

    def getHeaders( self, uri ):
        '''
        Returns a dict with the headers that would be used when sending a request
        to the remote server.
        '''
        req = urllib2.Request( uri )
        req = self._addHeaders( req )
        return req.headers
    
    def _isBlacklisted( self, uri ):
        '''
        If the user configured w3af to ignore a URL, we are going to be applying that configuration here.
        This is the lowest layer inside w3af.
        '''
        listOfNonTargets = cf.cf.getData('nonTargets') or []
        for u in listOfNonTargets:
            if urlParser.uri2url( uri ) == urlParser.uri2url( u ):
                om.out.debug('The URL you are trying to reach was configured as a non-target. ( '+uri+' ). Returning an empty response.')
                return True
        
        return False
    
    def sendRawRequest( self, head, postdata, fixContentLength=True, get_size=True):
        '''
        In some cases the xUrllib user wants to send a request that was typed in a textbox or is stored in a file.
        When something like that happens, this library allows the user to send the request by specifying two parameters
        for the sendRawRequest method:
        
        @parameter head: The postdata, if any. If set to '' or None, no postdata is sent.
        @parameter postdata: "<method> <URI> <HTTP version>\r\nHeader: Value\r\nHeader2: Value2..."
        @parameter fixContentLength: Indicates if the content length has to be fixed or not.
        
        @return: An httpResponse object.
        '''
        # Parse the two strings
        fuzzReq = httpRequestParser(head, postdata)
        
        # Fix the content length
        if fixContentLength:
            headers = fuzzReq.getHeaders()
            fixed = False
            for h in headers:
                if h.lower() == 'content-length':
                    headers[ h ] = str(len(postdata))
                    fixed = True
            if not fixed and postdata:
                headers[ 'content-length' ] = str(len(postdata))
            fuzzReq.setHeaders(headers)
        
        # Send it
        functionReference = getattr( self , fuzzReq.getMethod() )
        return functionReference( fuzzReq.getURI(), data=fuzzReq.getData(), headers=fuzzReq.getHeaders(),
                                                useCache=False, grepResult=False, getSize=get_size )
        
    def GET(self, uri, data='', headers={}, useCache=False, grepResult=True, getSize=False ):
        '''
        Gets a uri using a proxy, user agents, and other settings that where set previously.
        
        @param uri: This is the url to GET
        @param data: Only used if the uri parameter is really a URL.
        @return: An httpResponse object.
        '''
        self._init()

        if self._isBlacklisted( uri ):
            return httpResponse( NO_CONTENT, '', {}, uri, uri, msg='No Content' )
        
        qs = urlParser.getQueryString( uri )
        if qs:
            req = urllib2.Request( uri )
        else:
            if data:
                req = urllib2.Request( uri + '?' + data )
            else:
                # It's really an url...
                req = urllib2.Request( uri )
            
        req = self._addHeaders( req, headers )
        
        if getSize:
            # Check the file size
            try:
                self._checkFileSize( req )
            except sizeExceeded, se:
                return httpResponse( NO_CONTENT, '', {}, uri, uri, msg='No Content' )
            except Exception, e:
                raise e
        
        return self._send( req , useCache=useCache, grepResult=grepResult)
    
    def POST(self, uri, data='', headers={}, grepResult=True, getSize=False, useCache=False ):
        '''
        POST's data to a uri using a proxy, user agents, and other settings that where set previously.
        
        @param uri: This is the url where to post.
        @param data: A string with the data for the POST.
        @return: An httpResponse object.
        '''
        self._init()
        if self._isBlacklisted( uri ):
            return httpResponse( NO_CONTENT, '', {}, uri, uri, msg='No Content' )
        
        req = urllib2.Request(uri, data )
        req = self._addHeaders( req, headers )
        
        if getSize:
            # Check the file size
            try:
                self._checkFileSize( req )
            except sizeExceeded, se:
                return httpResponse( NO_CONTENT, '', {}, uri, uri, msg='No Content' )
            except Exception, e:
                raise e
        
        return self._send( req , grepResult=grepResult, useCache=useCache)
    
    def getRemoteFileSize( self, uri, headers={}, useCache=True ):
        '''
        @return: The file size of the remote file.
        '''
        res = self.HEAD( uri, headers=headers, useCache=useCache )  
        
        fileLen = None
        for i in res.getHeaders():
            if i.lower() == 'content-length':
                fileLen = res.getHeaders()[ i ]
                if fileLen.isdigit():
                    fileLen = int( fileLen )
                else:
                    msg = 'The content length header value of the response wasn\'t an integer... this is strange... The value is: ' + res.getHeaders()[ i ]
                    om.out.error( msg )
                    raise w3afException( msg )
        
        if fileLen != None:
            return fileLen
        else:
            om.out.debug( 'The response didn\'t contain a content-length header. Unable to return the remote file size of request with id: ' + str(res.id) )
            # I prefer to fetch the file, before this om.out.debug was a "raise w3afException", but this didnt make much sense
            return 0
        
    def __getattr__( self, methodName ):
        '''
        This is a "catch-all" way to be able to handle every HTTP method.
        '''
        class anyMethod:
            
            class methodRequest(urllib2.Request):
                def get_method(self):
                    return self._method
                def set_method( self, method ):
                    self._method = method
            
            def __init__( self, xu, method ):
                self._xurllib = xu
                self._method = method
            
            #(self, uri, data='', headers={}, useCache=False, grepResult=True, getSize=False )
            def __call__( self, *args, **keywords ):
                if len( args ) != 1:
                    msg = 'Invalid number of arguments. This method receives one argument and N keywords and you called it with:'
                    msg += 'args: ' + repr(args) + ' and keywords: ' + repr(keywords)
                    raise w3afException( msg )
                    
                uri = args[0]
                
                self._xurllib._init()
                
                if self._xurllib._isBlacklisted( uri ):
                    return httpResponse( NO_CONTENT, '', {}, uri, uri, 'No Content' )
            
                if 'data' in keywords:
                    req = self.methodRequest( uri, keywords['data'] )
                    keywords.pop('data')
                else:
                    req = self.methodRequest( uri )
                
                req.set_method( self._method )

                if 'headers' in keywords:
                    req = self._xurllib._addHeaders( req, keywords['headers'] )
                    keywords.pop('headers')
                
                if 'getSize' in keywords:
                    keywords.pop('getSize')
                    
                om.out.debug( req.get_method() + ' ' + uri)
                
                # def _send( self , req , useCache=False, useMultipart=False, grepResult=True )
                return apply(self._xurllib._send, (req,) , keywords )
        
        am = anyMethod( self, methodName )
        return am

    def _addHeaders( self , req, headers={} ):
        # Add all custom Headers if they exist
        for i in self.settings.HeaderList:
            req.add_header( i[0], i[1] )
        
        for h in headers.keys():
            req.add_header( h, headers[h] )

        return req
    
    def _checkURI( self, req ):
        #bug bug !
        #
        #[ Fri Sep 21 23:05:18 2007 - debug ] Reason: "unknown url type: javascript" , Exception: "<urlopen error unknown url type: javascript>"; going to retry.
        #[ Fri Sep 21 23:05:18 2007 - error ] Too many retries when trying to get: http://localhost/w3af/globalRedirect/2.php?url=javascript%3Aalert
        #
        ###TODO: The problem is that the urllib2 library fails even if i do this tests, it fails if it finds javascript: in some part of the URL    
        if req.get_full_url().startswith( 'http' ):
            return True
        elif req.get_full_url().startswith( 'javascript:' ) or req.get_full_url().startswith( 'mailto:' ):
            raise w3afException('Unsupported URL: ' +  req.get_full_url() )
        else:
            return False
    
    def _checkFileSize( self, req ):
        # No max file size.
        if self.settings.getMaxFileSize() == 0:
            pass
        else:
            # This will speed up the most frequent request, no HEAD is done to the last recently used URL's
            if req.get_full_url() not in self._sizeLRU:
                size = self.getRemoteFileSize( req.get_full_url() )
                self._sizeLRU[ req.get_full_url() ] = size
            else:
                size = self._sizeLRU[ req.get_full_url() ]
                #om.out.debug('Size of response got from self._sizeLRU.')
            
            if self.settings.getMaxFileSize() < size :
                msg = 'File size of URL: ' +  req.get_full_url()  + ' exceeds the configured file size limit. Max size limit is: ' + str(greek(self.settings.getMaxFileSize())) + ' and file size is: ' + str(greek(size)) + ' .'
                om.out.debug( msg )
                raise sizeExceeded( msg )
            
    def _send( self , req , useCache=False, useMultipart=False, grepResult=True ):
        # This is the place where I hook the pause and stop feature
        # And some other things like memory usage debugging.
        self._callBeforeSend()

        # Sanitize the URL
        self._checkURI( req )
        
        # Evasion
        original_url = req._Request__original
        req = self._evasion( req )
        
        startTime = time.time()
        res = None
        try:
            if useCache:
                res = self._cacheOpener.open( req )
            else:
                res = self._opener.open( req )
        except urllib2.URLError, e:
            # I get to this section of the code if a 400 error is returned
            # also possible when a proxy is configured and not available
            # also possible when auth credentials are wrong for the URI
            if hasattr(e, 'reason'):
                self._incrementGlobalErrorCount()
                try:
                    e.reason[0]
                except:
                    raise w3afException('Unexpected error in urllib2 / httplib: ' + repr(e.reason) )                    
                else:
                    if isinstance(e.reason, type(())):
                        # It's a tuple
                        if e.reason[0] in (-2, 111):
                            msg = 'w3af failed to reach the server while requesting: "' + original_url
                            msg += '".\nReason: "' + str(e.reason[1]) + '" , error code: "'
                            msg += str(e.reason[0]) + '".'
                            raise w3afException( msg )
                        else:
                            msg = 'w3af failed to reach the server while requesting: "' + original_url
                            msg += '".\nReason: "' + str(e.reason[1]) + '" , error code: "' + str(e.reason[0])
                            msg += '"; going to retry.'
                            om.out.debug( msg )
                            om.out.debug( 'Traceback for this error: ' + str( traceback.format_exc() ) )
                            req._Request__original = original_url
                            return self._retry( req, useCache )
                    else:
                        # Not a tuple, something "strange"!
                        msg = 'w3af failed to reach the server while requesting: "' + original_url
                        msg += '".\nReason: "' + str(e.reason)
                        msg += '"; going to retry.'
                        om.out.debug( msg )
                        om.out.debug( 'Traceback for this error: ' + str( traceback.format_exc() ) )
                        req._Request__original = original_url
                        return self._retry( req, useCache )

            elif hasattr(e, 'code'):
                # We usually get here when the response has codes 404, 403, 401, etc...
                msg = req.get_method() + ' ' + original_url +' returned HTTP code "'
                msg += str(e.code) + '" - id: ' + str(e.id)
                om.out.debug( msg )
                
                # Return this info to the caller
                code = int(e.code)
                info = e.info()
                geturl = e.geturl()
                read = self._readRespose( e )
                httpResObj = httpResponse(code, read, info, geturl, original_url, id=e.id, time=time.time() - startTime, msg=e.msg )
                
                # Clear the log of failed requests; this request is done!
                if id(req) in self._errorCount:
                    del self._errorCount[ id(req) ]
                self._zeroGlobalErrorCount()
            
                if grepResult:
                    self._grepResult( req, httpResObj )
                else:
                    om.out.debug('No grep for : ' + geturl + ' , the plugin sent grepResult=False.')
                return httpResObj
        except KeyboardInterrupt, k:
            # Correct control+c handling...
            raise k
        except Exception, e:
            # This except clause will catch errors like 
            # "(-3, 'Temporary failure in name resolution')"
            # "(-2, 'Name or service not known')"
            # The handling of this errors is complex... if I get a lot of errors in a row, I'll raise a
            # w3afMustStopException because the remote webserver might be unreachable.
            # For the first N errors, I just return an empty response...
            om.out.debug( req.get_method() + ' ' + original_url +' returned HTTP code "' + str(NO_CONTENT) + '"' )
            om.out.debug( 'Unhandled exception in xUrllib._send(): ' + str ( e ) )
            om.out.debug( str( traceback.format_exc() ) )
            
            # Clear the log of failed requests; this request is done!
            if id(req) in self._errorCount:
                del self._errorCount[ id(req) ]
            self._incrementGlobalErrorCount()
            
            return httpResponse( NO_CONTENT, '', {}, original_url, original_url, msg='No Content' )
        else:
            # Everything ok !
            if not req.get_data():
                msg = req.get_method() + ' ' + urllib.unquote_plus( original_url ) +' returned HTTP code "'
                msg += str(res.code) + '" - id: ' + str(res.id)
                om.out.debug( msg )
            else:
                msg = req.get_method() + ' ' + original_url +' with data: "'
                msg += urllib.unquote_plus( req.get_data() ) +'" returned HTTP code "'
                msg += str(res.code) + '" - id: ' + str(res.id)
                om.out.debug( msg )
            
            code = int(res.code)
            info = res.info()
            geturl = res.geturl()
            read = self._readRespose( res )
            httpResObj = httpResponse(code, read, info, geturl, original_url, id=res.id, time=time.time() - startTime, msg=res.msg )
            # Let the upper layers know that this response came from the local cache.
            if isinstance(res, CachedResponse):
                httpResObj.setFromCache(True)
            
            # Clear the log of failed requests; this request is done!
            if id(req) in self._errorCount:
                del self._errorCount[ id(req) ]
            self._zeroGlobalErrorCount()
            
            if grepResult:
                self._grepResult( req, httpResObj )
            else:
                om.out.debug('No grep for : ' + geturl + ' , the plugin sent grepResult=False.')
            return httpResObj

    def _readRespose( self, res ):
        read = ''
        try:
            read = res.read()
        except KeyboardInterrupt, k:
            raise k
        except Exception, e:
            om.out.error( str ( e ) )
            return read
        return read
        
    def _retry( self, req , useCache ):
        '''
        Try to send the request again while doing some error handling.
        '''
        if self._errorCount.get( id(req), 0 ) < self.settings.getMaxRetrys() :
            # Increment the error count of this particular request.
            if id(req) not in self._errorCount:
                self._errorCount[ id(req) ] = 0
            self._errorCount[ id(req) ] += 1
            
            om.out.debug('Re-sending request...')
            return self._send( req, useCache )
        else:
            # Clear the log of failed requests; this one definetly failed...
            del self._errorCount[ id(req) ]
            # No need to add this:
            #self._incrementGlobalErrorCount()
            # The global error count is already incremented by the _send method.
            raise w3afException('Too many retries when trying to get: ' + req.get_full_url() )
    
    def _incrementGlobalErrorCount( self ):
        '''
        Increment the error count, and if we got a lot of failures... raise a "afMustStopException"
        '''
        if self._ignore_errors_conf:
            return

        # All the logic follows:
        if self._lastRequestFailed:
            self._consecutiveErrorCount += 1
        else:
            self._lastRequestFailed = True
        
        om.out.debug('Incrementing global error count. GEC: ' + str(self._consecutiveErrorCount))
        
        if self._consecutiveErrorCount >= 10:
            msg = 'The xUrllib found too much consecutive errors. The remote webserver doesn\'t'
            msg += ' seem to be reachable anymore; please verify manually.'
            raise w3afMustStopException( msg )

    def ignore_errors( self, yes_no ):
        '''
        Let the library know if errors should be ignored or not. Basically,
        ignore all calls to "_incrementGlobalErrorCount" and don't raise the
        w3afMustStopException.

        @parameter yes_no: True to ignore errors.
        '''
        self._ignore_errors_conf = yes_no
            
    def _zeroGlobalErrorCount( self ):
        if self._lastRequestFailed or self._consecutiveErrorCount:
            self._lastRequestFailed = False
            self._consecutiveErrorCount = 0
            om.out.debug('Decrementing global error count. GEC: ' + str(self._consecutiveErrorCount))
    
    def setGrepPlugins(self, grepPlugins ):
        self._grepPlugins = grepPlugins
    
    def setEvasionPlugins( self, evasionPlugins ):
        # I'm sorting evasion plugins based on priority
        def sortFunc(x, y):
            return cmp(x.getPriority(), y.getPriority())
        evasionPlugins.sort(sortFunc)

        # Save the info
        self._evasionPlugins = evasionPlugins
        
    def _evasion( self, request ):
        '''
        @parameter request: urllib2.Request instance that is going to be modified by the evasion plugins
        '''
        for eplugin in self._evasionPlugins:
            try:
                request = eplugin.modifyRequest( request )
            except w3afException, e:
                om.out.error('Evasion plugin "'+eplugin.getName()+'" failed to modify the request. Exception: ' + str(e) )
            except Exception, e:
                raise e
                
        return request
        
    def _grepResult(self, request, response):
        # The grep process is all done in another thread. This improves the
        # speed of all w3af.
        if len( self._grepPlugins ) and urlParser.getDomain( request.get_full_url() ) in cf.cf.getData('targetDomains'):
            # I'll create a fuzzable request based on the urllib2 request object
            fuzzReq = createFuzzableRequestRaw( request.get_method(), request.get_full_url(), request.get_data(), request.headers )
            targs = (fuzzReq, response)
            self._tm.startFunction( target=self._grepWorker, args=targs, ownerObj=self, restrict=True )
    
    def _grepWorker( self , request, response):
        '''
        This method calls all the grep plugins so they can analyze the request/response pair.

        Remember that this method is always called in a thread (see the restrict=False 3 lines above)

        @parameter request: The request which generated the response. A request object.
        @parameter response: The response which was generated by the request (first parameter). A httpResponse object.
        '''
        om.out.debug('Starting grepWorker for response: ' + repr(response) )
        
        for grepPlugin in self._grepPlugins:
            try:
                grepPlugin.testResponse( request, response)
            except KeyboardInterrupt:
                # Correct control+c handling...
                raise
            except Exception, e:
                msg = 'Error in grep plugin, "' + grepPlugin.getName() + '" raised the exception: '
                msg += str(e) + '. Please report this bug to the w3af sourceforge project page '
                msg += '[ http://sourceforge.net/tracker/?func=add&group_id=170274&atid=853652 ] '
                msg += '\nException: ' + str(traceback.format_exc(1))
                om.out.error( msg )
                om.out.error( str(traceback.format_exc()) )
        
        om.out.debug('Finished grepWorker for response: ' + repr(response) )

_abbrevs = [
    (1<<50L, 'P'),
    (1<<40L, 'T'), 
    (1<<30L, 'G'), 
    (1<<20L, 'M'), 
    (1<<10L, 'k'),
    (1, '')
    ]

def greek(size):
    """
    Return a string representing the greek/metric suffix of a size
    """
    for factor, suffix in _abbrevs:
        if size > factor:
            break
    return str( int(size/factor) ) + suffix
    
