# Copyright 2016 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------------

"""
This module implements the Web server supporting the web api
"""

import logging
import os
import traceback
import copy

from twisted.internet import reactor
from twisted.internet import threads
from twisted.web import http, server
from twisted.web.error import Error
from twisted.web.resource import Resource
from twisted.web.server import Site
from twisted.web.static import File


from gossip.common import json2dict
from gossip.common import dict2json
from gossip.common import cbor2dict
from gossip.common import dict2cbor
from gossip.common import pretty_print_dict
from journal import global_store_manager
from journal import transaction
from journal.messages import transaction_message
from txnintegration.utils import PlatformStats
from txnserver.config import parse_listen_directives

from sawtooth.exceptions import InvalidTransactionError

logger = logging.getLogger(__name__)


class RootPage(Resource):
    isLeaf = True

    def __init__(self, validator):
        Resource.__init__(self)
        self.Ledger = validator.Ledger
        self.Validator = validator
        self.ps = PlatformStats()

        self.GetPageMap = {
            'block': self._handle_blk_request,
            'statistics': self._handle_stat_request,
            'store': self._handle_store_request,
            'transaction': self._handle_txn_request,
            'status': self._hdl_status_request,
        }

        self.PostPageMap = {
            'default': self._msg_forward,
            'forward': self._msg_forward,
            'initiate': self._msg_initiate,
            'command': self._do_command,
            'echo': self._msg_echo
        }

        static_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "static_content")
        self.static_content = File(static_dir)

    def error_response(self, request, response, *msgargs):
        """
        Generate a common error response for broken requests
        """
        request.setResponseCode(response)

        msg = msgargs[0].format(*msgargs[1:])
        if response > 400:
            logger.warn(msg)
        elif response > 300:
            logger.debug(msg)

        return "" if request.method == 'HEAD' else (msg + '\n')

    def errback(self, failure, request):
        failure.printTraceback()
        request.processingFailed(failure)
        return None

    def do_get(self, request):
        """
        Handle a GET request on the HTTP interface. Three paths are accepted:
            /store[/<storename>[/<key>|*]]
            /block[/<blockid>]
            /transaction[/<txnid>]
        """
        # pylint: disable=invalid-name

        # split the request path removing leading duplicate slashes
        components = request.path.split('/')
        while components and components[0] == '':
            components.pop(0)

        prefix = components.pop(0) if components else 'error'

        if prefix not in self.GetPageMap:
            # attempt to serve static content if present.
            resource = self.static_content.getChild(request.path[1:], request)
            return resource.render(request)

        test_only = (request.method == 'HEAD')

        try:
            response = self.GetPageMap[prefix](components, request.args,
                                               test_only)
            if test_only:
                return ''

            cbor = (request.getHeader('Accept') == 'application/cbor')

            if cbor:
                request.responseHeaders.addRawHeader(b"content-type",
                                                     b"application/cbor")
                return dict2cbor(response)

            request.responseHeaders.addRawHeader(b"content-type",
                                                 b"application/json")

            pretty = 'p' in request.args
            if pretty:
                result = pretty_print_dict(response) + '\n'
            else:
                result = dict2json(response)

            return result

        except Error as e:
            return self.error_response(
                request, int(e.status),
                'exception while processing http request {0}; {1}',
                request.path, str(e))

        except:
            logger.warn('error processing http request %s; %s', request.path,
                        traceback.format_exc(20))
            return self.error_response(request, http.BAD_REQUEST,
                                       'error processing http request {0}',
                                       request.path)

    def do_post(self, request):
        """
        Handle two types of HTTP POST requests:
         - gossip messages.  relayed to the gossip network as is
         - validator command and control (/command)
        """

        # break the path into its component parts

        components = request.path.split('/')
        while components and components[0] == '':
            components.pop(0)

        prefix = components.pop(0) if components else 'error'
        if prefix not in self.PostPageMap:
            prefix = 'default'

        encoding = request.getHeader('Content-Type')
        data = request.content.getvalue()

        # process non-gossip API requests
        if prefix == 'command':

            try:
                if encoding == 'application/json':
                    minfo = json2dict(data)
                else:
                    return self.error_response(request, http.BAD_REQUEST,
                                               'bad message encoding, {0}',
                                               encoding)
            except:
                logger.info('exception while decoding http request %s; %s',
                            request.path, traceback.format_exc(20))
                return self.error_response(
                    request, http.BAD_REQUEST,
                    'unable to decode incoming request {0}',
                    data)

            # process /command
            try:
                response = self.PostPageMap[prefix](request, components, minfo)
                request.responseHeaders.addRawHeader("content-type", encoding)
                result = dict2json(response)
                return result

            except Error as e:
                return self.error_response(
                    request, int(e.status),
                    'exception while processing request {0}; {1}',
                    request.path, str(e))

            except:
                logger.info('exception while processing http request %s; %s',
                            request.path, traceback.format_exc(20))
                return self.error_response(request, http.BAD_REQUEST,
                                           'error processing http request {0}',
                                           request.path)
        else:
            try:
                if encoding == 'application/json':
                    minfo = json2dict(data)
                elif encoding == 'application/cbor':
                    minfo = cbor2dict(data)
                else:
                    return self.error_response(request, http.BAD_REQUEST,
                                               'unknown message encoding, {0}',
                                               encoding)
                typename = minfo.get('__TYPE__', '**UNSPECIFIED**')
                if typename not in self.Ledger.MessageHandlerMap:
                    return self.error_response(
                        request, http.BAD_REQUEST,
                        'received request for unknown message type, {0}',
                        typename)

                msg = self.Ledger.MessageHandlerMap[typename][0](minfo)

            except:
                logger.info('exception while decoding http request %s; %s',
                            request.path, traceback.format_exc(20))
                return self.error_response(
                    request, http.BAD_REQUEST,
                    'unabled to decode incoming request {0}',
                    data)

            # determine if the message contains a valid transaction before
            # we send the message to the network

            # we need to start with a copy of the message due to cases
            # where side effects of the validity check may impact objects
            # related to msg
            mymsg = copy.deepcopy(msg)

            if hasattr(mymsg, 'Transaction') and mymsg.Transaction is not None:
                mytxn = mymsg.Transaction
                logger.info('starting local validation '
                            'for txn id: %s type: %s',
                            mytxn.Identifier,
                            mytxn.TransactionTypeName)
                block_id = self.Ledger.MostRecentCommittedBlockID

                real_store_map = self.Ledger.GlobalStoreMap.get_block_store(
                    block_id)
                temp_store_map = \
                    global_store_manager.BlockStore(real_store_map)
                if not temp_store_map:
                    logger.info('no store map for block %s', block_id)
                    return self.error_response(
                        request, http.BAD_REQUEST,
                        'unable to validate enclosed transaction {0}',
                        data)

                transaction_type = mytxn.TransactionTypeName
                if transaction_type not in temp_store_map.TransactionStores:
                    logger.info('transaction type %s not in global store map',
                                transaction_type)
                    return self.error_response(
                        request, http.BAD_REQUEST,
                        'unable to validate enclosed transaction {0}',
                        data)

                # clone a copy of the ledger's message queue so we can
                # temporarily play forward all locally submitted yet
                # uncommitted transactions
                my_queue = copy.deepcopy(self.Ledger.MessageQueue)

                # apply any enqueued messages
                while len(my_queue) > 0:
                    qmsg = my_queue.pop()
                    if qmsg and \
                       qmsg.MessageType in self.Ledger.MessageHandlerMap:
                        if (hasattr(qmsg, 'Transaction') and
                                qmsg.Transaction is not None):
                            my_store = temp_store_map.get_transaction_store(
                                qmsg.Transaction.TransactionTypeName)
                            if qmsg.Transaction.is_valid(my_store):
                                myqtxn = copy.copy(qmsg.Transaction)
                                myqtxn.apply(my_store)

                # apply any local pending transactions
                for txn_id in self.Ledger.PendingTransactions.iterkeys():
                    pend_txn = self.Ledger.TransactionStore[txn_id]
                    my_store = temp_store_map.get_transaction_store(
                        pend_txn.TransactionTypeName)
                    if pend_txn and pend_txn.is_valid(my_store):
                        my_pend_txn = copy.copy(pend_txn)
                        logger.debug('applying pending transaction '
                                     '%s to temp store', txn_id)
                        my_pend_txn.apply(my_store)

                # determine validity of the POSTed transaction against our
                # new temporary state
                my_store = temp_store_map.get_transaction_store(
                    mytxn.TransactionTypeName)
                try:
                    mytxn.check_valid(my_store)
                except InvalidTransactionError as e:
                    logger.info('submitted transaction fails transaction '
                                'family validation check: %s; %s',
                                request.path, mymsg.dump())
                    return self.error_response(
                        request, http.BAD_REQUEST,
                        "enclosed transaction failed transaction "
                        "family validation check: {}".format(str(e)),
                        data)
                except:
                    logger.info('submitted transaction is '
                                'not valid %s; %s; %s',
                                request.path, mymsg.dump(),
                                traceback.format_exc(20))
                    return self.error_response(
                        request, http.BAD_REQUEST,
                        "enclosed transaction is not valid",
                        data)

                logger.info('transaction %s is valid',
                            msg.Transaction.Identifier)

            # and finally execute the associated method
            # and send back the results
            try:
                response = self.PostPageMap[prefix](request, components, msg)

                request.responseHeaders.addRawHeader("content-type", encoding)
                if encoding == 'application/json':
                    result = dict2json(response.dump())
                else:
                    result = dict2cbor(response.dump())

                return result

            except Error as e:
                return self.error_response(
                    request, int(e.status),
                    'exception while processing request {0}; {1}',
                    request.path, str(e))

            except:
                logger.info('exception while processing http request %s; %s',
                            request.path, traceback.format_exc(20))
                return self.error_response(request, http.BAD_REQUEST,
                                           'error processing http request {0}',
                                           request.path)

    def final(self, message, request):
        request.write(message)
        try:
            request.finish()
        except RuntimeError:
            logger.error("No connection when request.finish called")

    def render_GET(self, request):
        # pylint: disable=invalid-name
        d = threads.deferToThread(self.do_get, request)
        d.addCallback(self.final, request)
        d.addErrback(self.errback, request)
        return server.NOT_DONE_YET

    def render_POST(self, request):
        # pylint: disable=invalid-name
        d = threads.deferToThread(self.do_post, request)
        d.addCallback(self.final, request)
        d.addErrback(self.errback, request)
        return server.NOT_DONE_YET

    def _msg_forward(self, request, components, msg):
        """
        Forward a signed message through the gossip network.
        """
        self.Ledger.handle_message(msg)
        return msg

    def _msg_initiate(self, request, components, msg):
        """
        Sign and echo a message
        """

        if request.getClientIP() != '127.0.0.1':
            raise Error(http.NOT_ALLOWED,
                        '{0} not authorized for message initiation'.format(
                            request.getClientIP()))

        if isinstance(msg, transaction_message.TransactionMessage):
            msg.Transaction.sign_from_node(self.Ledger.LocalNode)
        msg.sign_from_node(self.Ledger.LocalNode)

        self.Ledger.handle_message(msg)
        return msg

    def _msg_echo(self, request, components, msg):
        """
        Sign and echo a message
        """
        return msg

    def _do_command(self, request, components, cmd):
        """
        Process validator control commands
        """
        if cmd['action'] == 'start':
            if self.Validator.delaystart is True:
                self.Validator.delaystart = False
                logger.info("command received : %s", cmd['action'])
                cmd['action'] = 'started'
            else:
                logger.warn("validator startup not delayed")
                cmd['action'] = 'running'
        else:
            logger.warn("unknown command received")
            cmd['action'] = 'startup failed'

        return cmd

    def _handle_store_request(self, path_components, args, test_only):
        """
        Handle a store request. There are four types of requests:
            empty path -- return a list of known stores
            store name -- return a list of the keys in the store
            store name, key == '*' -- return a complete dump of all keys in the
                store
            store name, key != '*' -- return the data associated with the key
        """
        if not self.Ledger.GlobalStore:
            raise Error(http.BAD_REQUEST, 'no global store')

        block_id = self.Ledger.MostRecentCommittedBlockID
        if 'blockid' in args:
            block_id = args.get('blockid').pop(0)

        storemap = self.Ledger.GlobalStoreMap.get_block_store(block_id)
        if not storemap:
            raise Error(http.BAD_REQUEST,
                        'no store map for block <{0}>'.format(block_id))

        if len(path_components) == 0:
            return storemap.TransactionStores.keys()

        store_name = '/' + path_components.pop(0)
        if store_name not in storemap.TransactionStores:
            raise Error(http.BAD_REQUEST,
                        'no such store <{0}>'.format(store_name))

        store = storemap.get_transaction_store(store_name)

        if len(path_components) == 0:
            return store.keys()

        key = path_components[0]
        if key == '*':
            if 'delta' in args and args.get('delta').pop(0) == '1':
                return store.dump(True)
            return store.compose()

        if key not in store:
            raise Error(http.BAD_REQUEST, 'no such key {0}'.format(key))

        return store[key]

    def _handle_blk_request(self, path_components, args, test_only):
        """
        Handle a block request. There are three types of requests:
            empty path -- return a list of the committed block ids
            blockid -- return the contents of the specified block
            blockid and fieldname -- return the specific field within the block

        The request may specify additional parameters:
            blockcount -- the total number of blocks to return (newest to
                oldest)

        Blocks are returned newest to oldest.
        """

        if not path_components:
            count = 0
            if 'blockcount' in args:
                count = int(args.get('blockcount').pop(0))

            block_ids = self.Ledger.committed_block_ids(count)
            return block_ids

        block_id = path_components.pop(0)
        if block_id not in self.Ledger.BlockStore:
            raise Error(http.BAD_REQUEST, 'unknown block {0}'.format(block_id))

        binfo = self.Ledger.BlockStore[block_id].dump()
        binfo['Identifier'] = block_id

        if not path_components:
            return binfo

        field = path_components.pop(0)
        if field not in binfo:
            raise Error(http.BAD_REQUEST,
                        'unknown block field {0}'.format(field))

        return binfo[field]

    def _handle_txn_request(self, path_components, args, test_only):
        """
        Handle a transaction request. There are four types of requests:
            empty path -- return a list of the committed transactions ids
            txnid -- return the contents of the specified transaction
            txnid and field name -- return the contents of the specified
                transaction
            txnid and HEAD request -- return success only if the transaction
                                      has been committed
                404 -- transaction does not exist
                302 -- transaction exists but has not been committed
                200 -- transaction has been committed

        The request may specify additional parameters:
            blockcount -- the number of blocks (newest to oldest) from which to
                pull txns

        Transactions are returned from oldest to newest.
        """
        if len(path_components) == 0:
            blkcount = 0
            if 'blockcount' in args:
                blkcount = int(args.get('blockcount').pop(0))

            txnids = []
            blockids = self.Ledger.committed_block_ids(blkcount)
            while blockids:
                blockid = blockids.pop()
                txnids.extend(self.Ledger.BlockStore[blockid].TransactionIDs)
            return txnids

        txnid = path_components.pop(0)

        if txnid not in self.Ledger.TransactionStore:
            raise Error(http.NOT_FOUND,
                        'no such transaction {0}'.format(txnid))

        txn = self.Ledger.TransactionStore[txnid]

        if test_only:
            if txn.Status == transaction.Status.committed:
                return None
            else:
                raise Error(http.FOUND,
                            'transaction not committed {0}'.format(txnid))

        tinfo = txn.dump()
        tinfo['Identifier'] = txnid
        tinfo['Status'] = txn.Status
        if txn.Status == transaction.Status.committed:
            tinfo['InBlock'] = txn.InBlock

        if not path_components:
            return tinfo

        field = path_components.pop(0)
        if field not in tinfo:
            raise Error(http.BAD_REQUEST,
                        'unknown transaction field {0}'.format(field))

        return tinfo[field]

    def _handle_stat_request(self, path_components, args, testonly):
        if not path_components:
            raise Error(http.BAD_REQUEST, 'missing stat family')

        result = dict()

        source = path_components.pop(0)
        if source == 'ledger':
            for domain in self.Ledger.StatDomains.iterkeys():
                result[domain] = self.Ledger.StatDomains[domain].get_stats()
            return result
        if source == 'node':
            for peer in self.Ledger.NodeMap.itervalues():
                result[peer.Name] = peer.Stats.get_stats()
                result[peer.Name]['IsPeer'] = peer.is_peer
            return result
        if source == 'platform':
            result['platform'] = self.ps.get_data_as_dict()
            return result
        if source == 'all':
            for domain in self.Ledger.StatDomains.iterkeys():
                result[domain] = self.Ledger.StatDomains[domain].get_stats()
            for peer in self.Ledger.NodeMap.itervalues():
                result[peer.Name] = peer.Stats.get_stats()
                result[peer.Name]['IsPeer'] = peer.is_peer
            result['platform'] = self.ps.get_data_as_dict()
            return result

        if 'ledger' in args:
            for domain in self.Ledger.StatDomains.iterkeys():
                result[domain] = self.Ledger.StatDomains[domain].get_stats()
        if 'node' in args:
            for peer in self.Ledger.NodeMap.itervalues():
                result[peer.Name] = peer.Stats.get_stats()
                result[peer.Name]['IsPeer'] = peer.is_peer
        if 'platform' in args:
            result['platform'] = self.ps.get_data_as_dict()

        elif ('ledger' not in args) & ('node' not in args) \
                & ('platform' not in args):
            raise Error(http.NOT_FOUND, 'not valid source or arg')

        return result

    def _hdl_status_request(self, pathcomponents, args, testonly):
        result = dict()
        result['Status'] = self.Validator.status
        result['Domain'] = self.Validator.EndpointDomain
        result['Name'] = self.Ledger.LocalNode.Name
        result['HttpPort'] = self.Validator.Config.get('HttpPort', None)
        result['Host'] = self.Ledger.LocalNode.NetHost
        result['NodeIdentifier'] = self.Ledger.LocalNode.Identifier
        result['Port'] = self.Ledger.LocalNode.NetPort
        result['Peers'] = [x.Name
                           for x in self.Ledger.peer_list(allflag=False)]
        return result


class ApiSite(Site):
    """
    Override twisted.web.server.Site in order to remove the server header from
    each response.
    """

    def getResourceFor(self, request):
        """
        Remove the server header from the response.
        """
        request.responseHeaders.removeHeader('server')
        return Site.getResourceFor(self, request)


def initialize_web_server(config, validator):
    # Parse the listen directives from the configuration so
    # we know what to bind HTTP protocol to
    listen_directives = parse_listen_directives(config)

    if 'http' in listen_directives:
        root = RootPage(validator)
        site = ApiSite(root)
        interface = listen_directives['http'].host
        if interface is None:
            interface = ''
        logger.info(
            "listen for HTTP requests on (ip='%s', port=%s)",
            interface,
            listen_directives['http'].port)
        reactor.listenTCP(
            listen_directives['http'].port,
            site,
            interface=interface)
