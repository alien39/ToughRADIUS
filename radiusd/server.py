#!/usr/bin/env python
#coding=utf-8
# from twisted.internet import kqreactor
# kqreactor.install()
import sys,os
sys.path.insert(0,os.path.split(__file__)[0])
sys.path.insert(0,os.path.abspath(os.path.pardir))
from twisted.internet import task
from twisted.internet.defer import Deferred
from twisted.internet import protocol
from twisted.internet import reactor
from twisted.python import log
from pyrad import dictionary
from pyrad import host
from pyrad import packet
from store import store
from admin import UserTrace,AdminServerProtocol
from settings import auth_plugins,acct_plugins,acct_before_plugins
from plugins import *
import datetime
import middleware
import settings
import statistics
import logging
import six
import pprint
import utils
import json
import cache
import os

###############################################################################
# Basic Defined                                                            ####
###############################################################################

        
class PacketError(Exception):pass

class RADIUS(host.Host, protocol.DatagramProtocol):
    def __init__(self, 
                dict=None,
                trace=None,
                midware=None,
                runstat=None,
                debug=False):
        _dict = dictionary.Dictionary(dict)
        host.Host.__init__(self,dict=_dict)
        self.debug = debug
        self.user_trace = trace
        self.midware = midware
        self.runstat = runstat
        self.auth_delay = utils.AuthDelay(int(store.get_param("reject_delay") or 0))

    def processPacket(self, pkt):
        pass

    def createPacket(self, **kwargs):
        raise NotImplementedError('Attempted to use a pure base class')

    def datagramReceived(self, datagram, (host, port)):
        bas = store.get_bas(host)
        if not bas:
            return log.msg('Dropping packet from unknown host ' + host,level=logging.DEBUG)
        secret,vendor_id = bas['bas_secret'],bas['vendor_id']
        try:
            _packet = self.createPacket(packet=datagram,dict=self.dict,secret=six.b(str(secret)),vendor_id=vendor_id)
            _packet.deferred.addCallbacks(self.reply,self.on_exception)
            _packet.source = (host, port)
            log.msg("::Received radius request: %s"%(str(_packet)),level=logging.INFO)
            if self.debug:
                log.msg(_packet.format_str(),level=logging.DEBUG)    
            self.processPacket(_packet)
        except packet.PacketError as err:
            log.err(err,'::Dropping invalid packet from %s: %s'%((host, port),str(err)))

    def reply(self,reply):
        log.msg("send radius response: %s"%(reply),level=logging.INFO)
        if self.debug:
            log.msg(reply.format_str(),level=logging.DEBUG)
        self.transport.write(reply.ReplyPacket(), reply.source)  
        if reply.code == packet.AccessReject:
            self.runstat.auth_reject += 1
        elif reply.code == packet.AccessAccept:
            self.runstat.auth_accept += 1
 
    def on_exception(self,err):
        log.msg('Packet process error：%s' % str(err))   

    def process_delay(self):
        while self.auth_delay.delay_len() > 0:
            try:
                reject = self.auth_delay.get_delay_reject(0)
                if (datetime.datetime.now() - reject.created).seconds < self.auth_delay.reject_delay:
                    return
                else:
                    self.reply(self.auth_delay.pop_delay_reject())
            except:
                log.err("process_delay error")

###############################################################################
# Auth Server                                                              ####
###############################################################################
class RADIUSAccess(RADIUS):

    def createPacket(self, **kwargs):
        vendor_id = 0
        if 'vendor_id' in kwargs:
            vendor_id = kwargs.pop('vendor_id')
        pkt = utils.AuthPacket2(**kwargs)
        pkt.vendor_id = vendor_id
        return pkt

    def processPacket(self, req):
        self.runstat.auth_all += 1
        if req.code != packet.AccessRequest:
            self.runstat.auth_drop += 1
            raise PacketError('non-AccessRequest packet on authentication socket')
        
        reply = req.CreateReply()
        reply.source = req.source
        user = store.get_user(req.get_user_name())
        if user:self.user_trace.push(user['account_number'],req)
        # middleware execute
        for plugin in auth_plugins:
            self.midware.process(plugin,req=req,resp=reply,user=user)
            if reply.code == packet.AccessReject:
                self.auth_delay.add_roster(req.get_mac_addr())
                if user:self.user_trace.push(user['account_number'],reply)
                if self.auth_delay.over_reject(req.get_mac_addr()):
                    return self.auth_delay.add_delay_reject(reply)
                else:
                    return req.deferred.callback(reply)
                    
        # send accept
        reply['Reply-Message'] = 'success!'
        reply.code=packet.AccessAccept
        if user:self.user_trace.push(user['account_number'],reply)
        self.auth_delay.del_roster(req.get_mac_addr())
        req.deferred.callback(reply)
        
        
###############################################################################
# Acct Server                                                              ####
############################################################################### 
class RADIUSAccounting(RADIUS):

    def createPacket(self, **kwargs):
        vendor_id = 0
        if 'vendor_id' in kwargs:
            vendor_id = kwargs.pop('vendor_id')
        pkt = utils.AcctPacket2(**kwargs)
        pkt.vendor_id = vendor_id
        return pkt

    def processPacket(self, req):
        self.runstat.acct_all += 1
        if req.code != packet.AccountingRequest:
            self.runstat.acct_drop += 1
            raise PacketError('non-AccountingRequest packet on authentication socket')

        for plugin in acct_before_plugins:
            self.midware.process(plugin,req=req)
                 
        user = store.get_user(req.get_user_name())
        if user:self.user_trace.push(user['account_number'],req)        
          
        reply = req.CreateReply()
        reply.source = req.source
        if user:self.user_trace.push(user['account_number'],reply)   
        req.deferred.callback(reply)
        # middleware execute
        for plugin in acct_plugins:
            self.midware.process(plugin,req=req,user=user,runstat=self.runstat)
                

###############################################################################
# Run  Server                                                              ####
###############################################################################     
                 
def main():
    import argparse,json
    from twisted.python.logfile import DailyLogFile
    parser = argparse.ArgumentParser()
    parser.add_argument('-dict','--dictfile', type=str,default='dict/dictionary',dest='dictfile',help='dict file')
    parser.add_argument('-auth','--authport', type=int,default=1812,dest='authport',help='auth port')
    parser.add_argument('-acct','--acctport', type=int,default=1813,dest='acctport',help='acct port')
    parser.add_argument('-admin','--adminport', type=int,default=1815,dest='adminport',help='admin port')
    parser.add_argument('-c','--conf', type=str,default=None,dest='conf',help='conf file')
    parser.add_argument('-d','--debug', nargs='?',type=bool,default=False,dest='debug',help='debug')
    print sys.argv
    args =  parser.parse_args(sys.argv[1:])

    if args.conf:
        with open(args.conf) as cf:
            settings.db_config.update(**json.loads(cf.read()))

    if not args.debug:
        print 'logging to file logs/radiusd.log'
        log.startLogging(DailyLogFile.fromFullPath("./logs/radiusd.log"))
    else:
        log.startLogging(sys.stdout)

    _trace = UserTrace()
    _runstat = statistics.RunStat()
    _middleware = middleware.Middleware()
    _debug = args.debug or settings.debug

    def start_servers():
        auth_protocol = RADIUSAccess(
            dict=args.dictfile,
            trace=_trace,
            midware=_middleware,
            runstat=_runstat,
            debug=_debug
        )
        acct_protocol = RADIUSAccounting(
            dict=args.dictfile,
            trace=_trace,
            midware=_middleware,
            runstat=_runstat,
            debug=_debug
        )
        reactor.listenUDP(args.authport, auth_protocol)
        reactor.listenUDP(args.acctport, acct_protocol)
        _task = task.LoopingCall(auth_protocol.process_delay)
        _task.start(2.7)
        _cache_task = task.LoopingCall(cache.clear)
        _cache_task.start(3600)

        from autobahn.twisted.websocket import WebSocketServerFactory
        factory = WebSocketServerFactory("ws://0.0.0.0:%s"%args.adminport, debug = _debug)
        factory.protocol = AdminServerProtocol
        factory.protocol.user_trace = _trace
        factory.protocol.midware = _middleware
        factory.protocol.runstat = _runstat
        reactor.listenTCP(args.adminport, factory)

    start_servers()
    reactor.run()


if __name__ == '__main__':
    main()