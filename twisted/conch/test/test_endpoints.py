# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Tests for L{twisted.conch.endpoints}.
"""

import os.path
from struct import pack
from errno import ENOSYS

from zope.interface.verify import verifyObject, verifyClass
from zope.interface import implementer

from twisted.python.log import msg
from twisted.python.filepath import FilePath
from twisted.python.failure import Failure
from twisted.internet.interfaces import IAddress, IStreamClientEndpoint
from twisted.internet.protocol import Factory, Protocol
from twisted.internet.defer import CancelledError, Deferred, succeed, fail
from twisted.internet.error import ConnectionDone, ConnectionRefusedError
from twisted.internet.address import IPv4Address
from twisted.trial.unittest import TestCase
from twisted.test.proto_helpers import MemoryReactorClock
from twisted.internet.error import ProcessTerminated, ConnectingCancelledError

from twisted.cred.portal import Portal
from twisted.cred.checkers import InMemoryUsernamePasswordDatabaseDontUse

from twisted.conch.interfaces import IConchUser
from twisted.conch.error import ConchError, UserRejectedKey, HostKeyChanged

try:
    from Crypto.Cipher import AES
    from pyasn1 import type
except ImportError as e:
    skip = "can't run w/o PyCrypto and pyasn1 (%s)" % (e,)
    SSHFactory = SSHUserAuthServer = SSHConnection = Key = SSHChannel = \
        SSHAgentServer = KnownHostsFile = SSHPublicKeyDatabase = ConchUser = \
        object
else:
    from twisted.conch.ssh.factory import SSHFactory
    from twisted.conch.ssh.userauth import SSHUserAuthServer
    from twisted.conch.ssh.connection import SSHConnection
    from twisted.conch.ssh.keys import Key
    from twisted.conch.ssh.channel import SSHChannel
    from twisted.conch.ssh.agent import SSHAgentServer
    from twisted.conch.client.knownhosts import KnownHostsFile, ConsoleUI
    from twisted.conch.checkers import SSHPublicKeyDatabase
    from twisted.conch.avatar import ConchUser

    from twisted.conch.test.keydata import (
        publicRSA_openssh, privateRSA_openssh, privateDSA_openssh)

    from twisted.conch.endpoints import (
        _ISSHConnectionCreator, AuthenticationFailed, SSHCommandAddress,
        SSHCommandClientEndpoint, _ReadFile, _NewConnectionHelper,
        _ExistingConnectionHelper)

    from twisted.conch.ssh.transport import SSHClientTransport

from twisted.python.fakepwd import UserDatabase
from twisted.test.proto_helpers import StringTransport
from twisted.test.iosim import FakeTransport, connect


class AbortableFakeTransport(FakeTransport):
    """
    A L{FakeTransport} with added C{abortConnection} support.
    """
    aborted = False


    def abortConnection(self):
        """
        Abort the connection in a fake manner.

        This should really be implemented in the underlying module.
        """
        self.aborted = True



class BrokenExecSession(SSHChannel):
    """
    L{BrokenExecSession} is a session on which exec requests always fail.
    """
    def request_exec(self, data):
        """
        Fail all exec requests.

        @param data: Information about what is being executed.
        @type data: L{bytes}

        @return: C{0} to indicate failure
        @rtype: L{int}
        """
        return 0



class WorkingExecSession(SSHChannel):
    """
    L{WorkingExecSession} is a session on which exec requests always succeed.
    """
    def request_exec(self, data):
        """
        Succeed all exec requests.

        @param data: Information about what is being executed.
        @type data: L{bytes}

        @return: C{1} to indicate success
        @rtype: L{int}
        """
        return 1



class UnsatisfiedExecSession(SSHChannel):
    """
    L{UnsatisfiedExecSession} is a session on which exec requests are always
    delayed indefinitely, never succeeding or failing.
    """
    def request_exec(self, data):
        """
        Delay all exec requests indefinitely.

        @param data: Information about what is being executed.
        @type data: L{bytes}

        @return: A L{Deferred} which will never fire.
        @rtype: L{Deferred}
        """
        return Deferred()



class TrivialRealm(object):
    def __init__(self):
        self.channelLookup = {}


    def requestAvatar(self, avatarId, mind, *interfaces):
        avatar = ConchUser()
        avatar.channelLookup = self.channelLookup
        return (IConchUser, avatar, lambda: None)



class AddressSpyFactory(Factory):
    address = None

    def buildProtocol(self, address):
        self.address = address
        return Factory.buildProtocol(self, address)



class FixedResponseUI(object):
    def __init__(self, result):
        self.result = result


    def prompt(self, text):
        return succeed(self.result)


    def warn(self, text):
        pass



class FakeClockSSHUserAuthServer(SSHUserAuthServer):

    # Delegate this setting to the factory to simplify tweaking it
    @property
    def attemptsBeforeDisconnect(self):
        """
        Use the C{attemptsBeforeDisconnect} value defined by the factory to make
        it easier to override.
        """
        return self.transport.factory.attemptsBeforeDisconnect


    @property
    def clock(self):
        """
        Use the reactor defined by the factory, rather than the default global
        reactor, to simplify testing (by allowing an alternate implementation
        to be supplied by tests).
        """
        return self.transport.factory.reactor



class CommandFactory(SSHFactory):
    @property
    def publicKeys(self):
        return {
            'ssh-rsa': Key.fromString(data=publicRSA_openssh)
            }

    @property
    def privateKeys(self):
        return {
            'ssh-rsa': Key.fromString(data=privateRSA_openssh)
            }

    services = {
        'ssh-userauth': FakeClockSSHUserAuthServer,
        'ssh-connection': SSHConnection
    }

    # Simplify the tests by disconnecting after the first authentication
    # failure.  One attempt should be sufficient to test authentication success
    # and failure.  There is an off-by-one in the implementation of this
    # feature in Conch, so set it to 0 in order to allow 1 attempt.
    attemptsBeforeDisconnect = 0


@implementer(IAddress)
class MemoryAddress(object):
    pass



@implementer(IStreamClientEndpoint)
class SingleUseMemoryEndpoint(object):
    """
    L{SingleUseMemoryEndpoint} is a client endpoint which allows one connection
    to be set up and then exposes an API for moving around bytes related to
    that connection.

    @ivar pump: L{None} until a connection is attempted, then a L{IOPump}
        instance associated with the protocol which is connected.
    @type pump: L{IOPump}
    """
    def __init__(self, server):
        """
        @param server: An L{IProtocol} provider to which the client will be
            connected.
        @type server: L{IProtocol} provider
        """
        self.pump = None
        self._server = server


    def connect(self, factory):
        if self.pump is not None:
            raise Exception("SingleUseMemoryEndpoint was already used")

        try:
            protocol = factory.buildProtocol(MemoryAddress())
        except:
            return fail()
        else:
            self.pump = connect(
                self._server, AbortableFakeTransport(
                    self._server, isServer=True),
                protocol, AbortableFakeTransport(protocol, isServer=False))
            return succeed(protocol)



class MemorySSHPublicKeyDatabase(SSHPublicKeyDatabase):
    def __init__(self, users):
        self._users = users
        self._userdb = UserDatabase()
        for i, username in enumerate(self._users):
            self._userdb.addUser(
                username, b"garbage", 123 + i, 456, None, None, None)


    def getAuthorizedKeysFiles(self, credentials):
        try:
            return self._users[credentials.username]
        except KeyError:
            return []



class SSHCommandClientEndpointTestsMixin(object):
    """
    Tests for L{SSHCommandClientEndpoint}, an L{IStreamClientEndpoint}
    implementations which connects a protocol with the stdin and stdout of a
    command running in an SSH session.

    These tests apply to L{SSHCommandClientEndpoint} whether it is constructed
    using L{SSHCommandClientEndpoint.existingConnection} or
    L{SSHCommandClientEndpoint.newConnection}.

    Subclasses must override L{create}, L{assertClientTransportState}, and
    L{finishConnection}.
    """
    def setUp(self):
        self.hostname = b"ssh.example.com"
        self.port = 42022
        self.user = b"user"
        self.password = b"password"
        self.reactor = MemoryReactorClock()
        self.realm = TrivialRealm()
        self.portal = Portal(self.realm)
        self.passwdDB = InMemoryUsernamePasswordDatabaseDontUse()
        self.passwdDB.addUser(self.user, self.password)
        self.portal.registerChecker(self.passwdDB)
        self.factory = CommandFactory()
        self.factory.reactor = self.reactor
        self.factory.portal = self.portal
        self.factory.doStart()
        self.addCleanup(self.factory.doStop)

        self.clientAddress = IPv4Address("TCP", "10.0.0.1", 12345)
        self.serverAddress = IPv4Address("TCP", "192.168.100.200", 54321)


    def create(self):
        """
        Create and return a new L{SSHCommandClientEndpoint} to be tested.
        Override this to implement creation in an interesting way the endpoint.
        """
        raise NotImplementedError(
            "%r did not implement create" % (self.__class__.__name__,))


    def assertClientTransportState(self, client, immediateClose):
        """
        Make an assertion about the connectedness of the given protocol's
        transport.  Override this to implement either a check for the
        connection still being open or having been closed as appropriate.

        @param client: The client whose state is being checked.

        @param immediateClose: Boolean indicating whether the connection was
            closed immediately or not.
        """
        raise NotImplementedError(
            "%r did not implement assertClientTransportState" % (
                self.__class__.__name__,))


    def finishConnection(self):
        """
        Do any remaining work necessary to complete an in-memory connection
        attempted initiated using C{self.reactor}.
        """
        raise NotImplementedError(
            "%r did not implement finishConnection" % (
                self.__class__.__name__,))


    def connectedServerAndClient(self, serverFactory, clientFactory):
        """
        Set up an in-memory connection between protocols created by
        C{serverFactory} and C{clientFactory}.

        @return: A three-tuple.  The first element is the protocol created by
            C{serverFactory}.  The second element is the protocol created by
            C{clientFactory}.  The third element is the L{IOPump} connecting
            them.
        """
        clientProtocol = clientFactory.buildProtocol(None)
        serverProtocol = serverFactory.buildProtocol(None)

        clientTransport = AbortableFakeTransport(
            clientProtocol, isServer=False, hostAddress=self.clientAddress,
            peerAddress=self.serverAddress)
        serverTransport = AbortableFakeTransport(
            serverProtocol, isServer=True, hostAddress=self.serverAddress,
            peerAddress=self.clientAddress)

        pump = connect(
            serverProtocol, serverTransport, clientProtocol, clientTransport)
        return serverProtocol, clientProtocol, pump


    def test_interface(self):
        """
        L{SSHCommandClientEndpoint} instances provide L{IStreamClientEndpoint}.
        """
        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"dummy command", b"dummy user",
            self.hostname, self.port)
        self.assertTrue(verifyObject(IStreamClientEndpoint, endpoint))


    def test_channelOpenFailure(self):
        """
        If a channel cannot be opened on the authenticated SSH connection, the
        L{Deferred} returned by L{SSHCommandClientEndpoint.connect} fires with
        a L{Failure} wrapping the reason given by the server.
        """
        endpoint = self.create()

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.finishConnection()

        # The server logs the channel open failure - this is expected.
        errors = self.flushLoggedErrors(ConchError)
        self.assertIn(
            'unknown channel', (errors[0].value.data, errors[0].value.value))
        self.assertEqual(1, len(errors))

        # Now deal with the results on the endpoint side.
        f = self.failureResultOf(connected)
        f.trap(ConchError)
        self.assertEqual('unknown channel', f.value.value)

        self.assertClientTransportState(client, False)


    def test_execFailure(self):
        """
        If execution of the command fails, the L{Deferred} returned by
        L{SSHCommandClientEndpoint.connect} fires with a L{Failure} wrapping
        the reason given by the server.
        """
        self.realm.channelLookup[b'session'] = BrokenExecSession
        endpoint = self.create()

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.finishConnection()

        f = self.failureResultOf(connected)
        f.trap(ConchError)
        self.assertEqual('channel request failed', f.value.value)

        self.assertClientTransportState(client, False)


    def test_execCancelled(self):
        """
        If execution of the command is cancelled via the L{Deferred} returned
        by L{SSHCommandClientEndpoint.connect}, the connection is closed
        immediately.
        """
        self.realm.channelLookup[b'session'] = UnsatisfiedExecSession
        endpoint = self.create()

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)
        server, client, pump = self.finishConnection()

        connected.cancel()

        f = self.failureResultOf(connected)
        f.trap(CancelledError)

        self.assertClientTransportState(client, True)


    def test_buildProtocol(self):
        """
        Once the necessary SSH actions have completed successfully,
        L{SSHCommandClientEndpoint.connect} uses the factory passed to it to
        construct a protocol instance by calling its C{buildProtocol} method
        with an address object representing the SSH connection and command
        executed.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = self.create()

        factory = AddressSpyFactory()
        factory.protocol = Protocol

        endpoint.connect(factory)

        server, client, pump = self.finishConnection()

        self.assertIsInstance(factory.address, SSHCommandAddress)
        self.assertEqual(server.transport.getHost(), factory.address.server)
        self.assertEqual(self.user, factory.address.username)
        self.assertEqual(b"/bin/ls -l", factory.address.command)


    def test_makeConnection(self):
        """
        L{SSHCommandClientEndpoint} establishes an SSH connection, creates a
        channel in it, runs a command in that channel, and uses the protocol's
        C{makeConnection} to associate it with a protocol representing that
        command's stdin and stdout.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = self.create()

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.finishConnection()

        protocol = self.successResultOf(connected)
        self.assertIsNot(None, protocol.transport)


    def test_dataReceived(self):
        """
        After establishing the connection, when the command on the SSH server
        produces output, it is delivered to the protocol's C{dataReceived}
        method.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = self.create()

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.finishConnection()

        protocol = self.successResultOf(connected)
        dataReceived = []
        protocol.dataReceived = dataReceived.append

        # Figure out which channel on the connection this protocol is
        # associated with so the test can do a write on it.
        channelId = protocol.transport.id

        server.service.channels[channelId].write(b"hello, world")
        pump.pump()
        self.assertEqual(b"hello, world", b"".join(dataReceived))


    def test_connectionLost(self):
        """
        When the command closes the channel, the protocol's C{connectionLost}
        method is called.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = self.create()

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.finishConnection()

        protocol = self.successResultOf(connected)
        connectionLost = []
        protocol.connectionLost = connectionLost.append

        # Figure out which channel on the connection this protocol is associated
        # with so the test can do a write on it.
        channelId = protocol.transport.id
        server.service.channels[channelId].loseConnection()

        pump.pump()
        connectionLost[0].trap(ConnectionDone)

        self.assertClientTransportState(client, False)


    def _exitStatusTest(self, request, requestArg):
        """
        Test handling of non-zero exit statuses or exit signals.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = self.create()

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.finishConnection()

        protocol = self.successResultOf(connected)
        connectionLost = []
        protocol.connectionLost = connectionLost.append

        # Figure out which channel on the connection this protocol is associated
        # with so the test can simulate command exit and channel close.
        channelId = protocol.transport.id
        channel = server.service.channels[channelId]

        server.service.sendRequest(channel, request, requestArg)
        channel.loseConnection()
        pump.pump()
        self.assertClientTransportState(client, False)
        return connectionLost[0]


    def test_zeroExitCode(self):
        """
        When the command exits with a non-zero status, the protocol's
        C{connectionLost} method is called with a L{Failure} wrapping an
        exception which encapsulates that status.
        """
        exitCode = 0
        exc = self._exitStatusTest('exit-status', pack('>L', exitCode))
        exc.trap(ConnectionDone)


    def test_nonZeroExitStatus(self):
        """
        When the command exits with a non-zero status, the protocol's
        C{connectionLost} method is called with a L{Failure} wrapping an
        exception which encapsulates that status.
        """
        exitCode = 123
        signal = None
        exc = self._exitStatusTest('exit-status', pack('>L', exitCode))
        exc.trap(ProcessTerminated)
        self.assertEqual(exitCode, exc.value.exitCode)
        self.assertEqual(signal, exc.value.signal)


    def test_nonZeroExitSignal(self):
        """
        When the command exits with a non-zero signal, the protocol's
        C{connectionLost} method is called with a L{Failure} wrapping an
        exception which encapsulates that status.
        """
        exitCode = None
        signal = 123
        exc = self._exitStatusTest('exit-signal', pack('>L', signal))
        exc.trap(ProcessTerminated)
        self.assertEqual(exitCode, exc.value.exitCode)
        self.assertEqual(signal, exc.value.signal)


    def record(self, server, protocol, event, noArgs=False):
        """
        Hook into and record events which happen to C{protocol}.

        @param server: The SSH server protocol over which C{protocol} is
            running.
        @type server: L{IProtocol} provider

        @param protocol:

        @param event:

        @param noArgs:
        """
        # Figure out which channel the test is going to send data over so we can
        # look for it to arrive at the right place on the server.
        channelId = protocol.transport.id

        recorder = []
        if noArgs:
            f = lambda: recorder.append(None)
        else:
            f = recorder.append

        setattr(server.service.channels[channelId], event, f)
        return recorder


    def test_write(self):
        """
        The transport connected to the protocol has a C{write} method which
        sends bytes to the input of the command executing on the SSH server.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = self.create()

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.finishConnection()

        protocol = self.successResultOf(connected)

        dataReceived = self.record(server, protocol, 'dataReceived')
        protocol.transport.write(b"hello, world")
        pump.pump()
        self.assertEqual(b"hello, world", b"".join(dataReceived))


    def test_writeSequence(self):
        """
        The transport connected to the protocol has a C{writeSequence} method which
        sends bytes to the input of the command executing on the SSH server.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = self.create()

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.finishConnection()

        protocol = self.successResultOf(connected)

        dataReceived = self.record(server, protocol, 'dataReceived')
        protocol.transport.writeSequence(list(b"hello, world"))
        pump.pump()
        self.assertEqual(b"hello, world", b"".join(dataReceived))



class NewConnectionTests(TestCase, SSHCommandClientEndpointTestsMixin):
    """
    Tests for L{SSHCommandClientEndpoint} when using the C{newConnection}
    constructor.
    """
    def setUp(self):
        """
        Configure an SSH server with password authentication enabled for a
        well-known (to the tests) account.
        """
        SSHCommandClientEndpointTestsMixin.setUp(self)
        # Make the server's host key available to be verified by the client.
        self.hostKeyPath = FilePath(self.mktemp())
        self.knownHosts = KnownHostsFile(self.hostKeyPath)
        self.knownHosts.addHostKey(
            self.hostname, self.factory.publicKeys['ssh-rsa'])
        self.knownHosts.addHostKey(
            self.serverAddress.host, self.factory.publicKeys['ssh-rsa'])
        self.knownHosts.save()


    def create(self):
        """
        Create and return a new L{SSHCommandClientEndpoint} using the
        C{newConnection} constructor.
        """
        return SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", self.user, self.hostname, self.port,
            password=self.password, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))


    def finishConnection(self):
        """
        Establish the first attempted TCP connection using the SSH server which
        C{self.factory} can create.
        """
        return self.connectedServerAndClient(
            self.factory, self.reactor.tcpClients[0][2])


    def assertClientTransportState(self, client, immediateClose):
        """
        Assert that the transport for the given protocol has been disconnected.
        L{SSHCommandClientEndpoint.newConnection} creates a new dedicated SSH
        connection and cleans it up after the command exits.
        """
        # Nothing useful can be done with the connection at this point, so the
        # endpoint should close it.
        if immediateClose:
            self.assertTrue(client.transport.aborted)
        else:
            self.assertTrue(client.transport.disconnecting)


    def test_destination(self):
        """
        L{SSHCommandClientEndpoint} uses the L{IReactorTCP} passed to it to
        attempt a connection to the host/port address also passed to it.
        """
        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", self.user, self.hostname, self.port,
            password=self.password, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))
        factory = Factory()
        factory.protocol = Protocol
        endpoint.connect(factory)

        host, port, factory, timeout, bindAddress = self.reactor.tcpClients[0]
        self.assertEqual(self.hostname, host)
        self.assertEqual(self.port, port)
        self.assertEqual(1, len(self.reactor.tcpClients))


    def test_connectionFailed(self):
        """
        If a connection cannot be established, the L{Deferred} returned by
        L{SSHCommandClientEndpoint.connect} fires with a L{Failure}
        representing the reason for the connection setup failure.
        """
        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", b"dummy user",
            self.hostname, self.port, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))
        factory = Factory()
        factory.protocol = Protocol
        d = endpoint.connect(factory)

        factory = self.reactor.tcpClients[0][2]
        factory.clientConnectionFailed(None, Failure(ConnectionRefusedError()))

        self.failureResultOf(d).trap(ConnectionRefusedError)


    def test_userRejectedHostKey(self):
        """
        If the L{KnownHostsFile} instance used to construct
        L{SSHCommandClientEndpoint} rejects the SSH public key presented by the
        server, the L{Deferred} returned by L{SSHCommandClientEndpoint.connect}
        fires with a L{Failure} wrapping L{UserRejectedKey}.
        """
        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", b"dummy user",
            self.hostname, self.port, knownHosts=KnownHostsFile(self.mktemp()),
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.reactor.tcpClients[0][2])

        f = self.failureResultOf(connected)
        f.trap(UserRejectedKey)


    def test_mismatchedHostKey(self):
        """
        If the SSH public key presented by the SSH server does not match the
        previously remembered key, as reported by the L{KnownHostsFile}
        instance use to construct the endpoint, for that server, the
        L{Deferred} returned by L{SSHCommandClientEndpoint.connect} fires with
        a L{Failure} wrapping L{HostKeyChanged}.
        """
        differentKey = Key.fromString(privateDSA_openssh).public()
        knownHosts = KnownHostsFile(self.mktemp())
        knownHosts.addHostKey(self.serverAddress.host, differentKey)
        knownHosts.addHostKey(self.hostname, differentKey)

        # The UI may answer true to any questions asked of it; they should
        # make no difference, since a *mismatched* key is not even optionally
        # allowed to complete a connection.
        ui = FixedResponseUI(True)

        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", b"dummy user",
            self.hostname, self.port, password=b"dummy password",
            knownHosts=knownHosts, ui=ui)

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.reactor.tcpClients[0][2])

        f = self.failureResultOf(connected)
        f.trap(HostKeyChanged)


    def test_connectionClosedBeforeSecure(self):
        """
        If the connection closes at any point before the SSH transport layer
        has finished key exchange (ie, gotten to the point where we may attempt
        to authenticate), the L{Deferred} returned by
        L{SSHCommandClientEndpoint.connect} fires with a L{Failure} wrapping
        the reason for the lost connection.
        """
        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", b"dummy user",
            self.hostname, self.port, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        d = endpoint.connect(factory)

        transport = StringTransport()
        factory = self.reactor.tcpClients[0][2]
        client = factory.buildProtocol(None)
        client.makeConnection(transport)

        client.connectionLost(Failure(ConnectionDone()))
        self.failureResultOf(d).trap(ConnectionDone)


    def test_connectionCancelledBeforeSecure(self):
        """
        If the connection is cancelled before the SSH transport layer has
        finished key exchange (ie, gotten to the point where we may attempt to
        authenticate), the L{Deferred} returned by
        L{SSHCommandClientEndpoint.connect} fires with a L{Failure} wrapping
        L{CancelledError} and the connection is aborted.
        """
        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", b"dummy user",
            self.hostname, self.port, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        d = endpoint.connect(factory)

        transport = AbortableFakeTransport(None, isServer=False)
        factory = self.reactor.tcpClients[0][2]
        client = factory.buildProtocol(None)
        client.makeConnection(transport)
        d.cancel()

        self.failureResultOf(d).trap(CancelledError)
        self.assertTrue(transport.aborted)
        # Make sure the connection closing doesn't result in unexpected
        # behavior when due to cancellation:
        client.connectionLost(Failure(ConnectionDone()))


    def test_connectionCancelledBeforeConnected(self):
        """
        If the connection is cancelled before it finishes connecting, the
        connection attempt is stopped.
        """
        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", b"dummy user",
            self.hostname, self.port, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        d = endpoint.connect(factory)
        d.cancel()
        self.failureResultOf(d).trap(ConnectingCancelledError)
        self.assertTrue(self.reactor.connectors[0].stoppedConnecting)


    def test_passwordAuthenticationFailure(self):
        """
        If the SSH server rejects the password presented during authentication,
        the L{Deferred} returned by L{SSHCommandClientEndpoint.connect} fires
        with a L{Failure} wrapping L{AuthenticationFailed}.
        """
        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", b"dummy user",
            self.hostname, self.port,  password=b"dummy password",
            knownHosts=self.knownHosts, ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.reactor.tcpClients[0][2])

        # For security, the server delays password authentication failure
        # response.  Advance the simulation clock so the client sees the
        # failure.
        self.reactor.advance(server.service.passwordDelay)

        # Let the failure response traverse the "network"
        pump.flush()

        f = self.failureResultOf(connected)
        f.trap(AuthenticationFailed)
        # XXX Should assert something specific about the arguments of the
        # exception

        self.assertClientTransportState(client, False)


    def setupKeyChecker(self, portal, users):
        """
        Create an L{ISSHPrivateKey} checker which recognizes C{users} and add it
        to C{portal}.

        @param portal: A L{Portal} to which to add the checker.
        @type portal: L{Portal}

        @param users: The users and their keys the checker will recognize.  Keys
            are byte strings giving user names.  Values are byte strings giving
            OpenSSH-formatted private keys.
        @type users: C{dict}
        """
        credentials = {}
        for username, keyString in users.iteritems():
            goodKey = Key.fromString(keyString)
            authorizedKeys = FilePath(self.mktemp())
            authorizedKeys.setContent(goodKey.public().toString("OPENSSH"))
            credentials[username] = [authorizedKeys]
        checker = MemorySSHPublicKeyDatabase(credentials)
        portal.registerChecker(checker)


    def test_publicKeyAuthenticationFailure(self):
        """
        If the SSH server rejects the key pair presented during authentication,
        the L{Deferred} returned by L{SSHCommandClientEndpoint.connect} fires
        with a L{Failure} wrapping L{AuthenticationFailed}.
        """
        badKey = Key.fromString(privateRSA_openssh)
        self.setupKeyChecker(self.portal, {self.user: privateDSA_openssh})

        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", self.user,
            self.hostname, self.port, keys=[badKey],
            knownHosts=self.knownHosts, ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.reactor.tcpClients[0][2])

        f = self.failureResultOf(connected)
        f.trap(AuthenticationFailed)
        # XXX Should assert something specific about the arguments of the
        # exception

        # Nothing useful can be done with the connection at this point, so the
        # endpoint should close it.
        self.assertTrue(client.transport.disconnecting)


    def test_authenticationFallback(self):
        """
        If the SSH server does not accept any of the specified SSH keys, the
        specified password is tried.
        """
        badKey = Key.fromString(privateRSA_openssh)
        self.setupKeyChecker(self.portal, {self.user: privateDSA_openssh})

        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", self.user, self.hostname, self.port,
            keys=[badKey], password=self.password, knownHosts=self.knownHosts,
            ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        # Exercising fallback requires a failed authentication attempt.  Allow
        # one.
        self.factory.attemptsBeforeDisconnect += 1

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.reactor.tcpClients[0][2])

        pump.pump()

        # The server logs the channel open failure - this is expected.
        errors = self.flushLoggedErrors(ConchError)
        self.assertIn(
            'unknown channel', (errors[0].value.data, errors[0].value.value))
        self.assertEqual(1, len(errors))

        # Now deal with the results on the endpoint side.
        f = self.failureResultOf(connected)
        f.trap(ConchError)
        self.assertEqual('unknown channel', f.value.value)

        # Nothing useful can be done with the connection at this point, so the
        # endpoint should close it.
        self.assertTrue(client.transport.disconnecting)


    def test_publicKeyAuthentication(self):
        """
        If L{SSHCommandClientEndpoint} is initialized with any private keys, it
        will try to use them to authenticate with the SSH server.
        """
        key = Key.fromString(privateDSA_openssh)
        self.setupKeyChecker(self.portal, {self.user: privateDSA_openssh})

        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", self.user, self.hostname, self.port,
            keys=[key], knownHosts=self.knownHosts, ui=FixedResponseUI(False))

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.reactor.tcpClients[0][2])

        protocol = self.successResultOf(connected)
        self.assertIsNot(None, protocol.transport)


    def test_agentAuthentication(self):
        """
        If L{SSHCommandClientEndpoint} is initialized with an
        L{SSHAgentClient}, the agent is used to authenticate with the SSH
        server.
        """
        key = Key.fromString(privateRSA_openssh)
        agentServer = SSHAgentServer()
        agentServer.factory = Factory()
        agentServer.factory.keys = {key.blob(): (key, "")}

        self.setupKeyChecker(self.portal, {self.user: privateRSA_openssh})

        agentEndpoint = SingleUseMemoryEndpoint(agentServer)
        endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", self.user, self.hostname, self.port,
            knownHosts=self.knownHosts, ui=FixedResponseUI(False),
            agentEndpoint=agentEndpoint)

        self.realm.channelLookup[b'session'] = WorkingExecSession

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.connectedServerAndClient(
            self.factory, self.reactor.tcpClients[0][2])

        # Let the agent client talk with the agent server and the ssh client
        # talk with the ssh server.
        for i in range(14):
            agentEndpoint.pump.pump()
            pump.pump()

        protocol = self.successResultOf(connected)
        self.assertIsNot(None, protocol.transport)


    def test_loseConnection(self):
        """
        The transport connected to the protocol has a C{loseConnection} method
        which causes the channel in which the command is running to close and
        the overall connection to be closed.
        """
        self.realm.channelLookup[b'session'] = WorkingExecSession
        endpoint = self.create()

        factory = Factory()
        factory.protocol = Protocol
        connected = endpoint.connect(factory)

        server, client, pump = self.finishConnection()

        protocol = self.successResultOf(connected)
        closed = self.record(server, protocol, 'closed', noArgs=True)
        protocol.transport.loseConnection()
        pump.pump()
        self.assertEqual([None], closed)

        # Let the last bit of network traffic flow.  This lets the server's
        # close acknowledgement through, at which point the client can close
        # the overall SSH connection.
        pump.pump()

        # Nothing useful can be done with the connection at this point, so the
        # endpoint should close it.
        self.assertTrue(client.transport.disconnecting)



class ExistingConnectionTests(TestCase, SSHCommandClientEndpointTestsMixin):
    """
    Tests for L{SSHCommandClientEndpoint} when using the C{existingConnection}
    constructor.
    """
    def setUp(self):
        """
        Configure an SSH server with password authentication enabled for a
        well-known (to the tests) account.
        """
        SSHCommandClientEndpointTestsMixin.setUp(self)

        knownHosts = KnownHostsFile(FilePath(self.mktemp()))
        knownHosts.addHostKey(
            self.hostname, self.factory.publicKeys['ssh-rsa'])
        knownHosts.addHostKey(
            self.serverAddress.host, self.factory.publicKeys['ssh-rsa'])

        self.endpoint = SSHCommandClientEndpoint.newConnection(
            self.reactor, b"/bin/ls -l", self.user, self.hostname, self.port,
            password=self.password, knownHosts=knownHosts,
            ui=FixedResponseUI(False))


    def create(self):
        """
        Create and return a new L{SSHCommandClientEndpoint} using the
        C{existingConnection} constructor.
        """
        factory = Factory()
        factory.protocol = Protocol
        connected = self.endpoint.connect(factory)

        # Please, let me in.  This kinda sucks.
        channelLookup = self.realm.channelLookup.copy()
        try:
            self.realm.channelLookup[b'session'] = WorkingExecSession

            server, client, pump = self.connectedServerAndClient(
                self.factory, self.reactor.tcpClients[0][2])

        finally:
            self.realm.channelLookup.clear()
            self.realm.channelLookup.update(channelLookup)

        self._server = server
        self._client = client
        self._pump = pump

        protocol = self.successResultOf(connected)
        connection = protocol.transport.conn
        return SSHCommandClientEndpoint.existingConnection(
            connection, b"/bin/ls -l")


    def finishConnection(self):
        """
        Give back the connection established in L{create} over which the new
        command channel being tested will exchange data.
        """
        # The connection was set up and the first command channel set up, but
        # some more I/O needs to happen for the second command channel to be
        # ready.  Make that I/O happen before giving back the objects.
        self._pump.pump()
        self._pump.pump()
        self._pump.pump()
        self._pump.pump()
        return self._server, self._client, self._pump


    def assertClientTransportState(self, client, immediateClose):
        """
        Assert that the transport for the given protocol is still connected.
        L{SSHCommandClientEndpoint.existingConnection} re-uses an SSH connected
        created by some other code, so other code is responsible for cleaning
        it up.
        """
        self.assertFalse(client.transport.disconnecting)
        self.assertFalse(client.transport.aborted)



class ExistingConnectionHelperTests(TestCase):
    """
    Tests for L{_ExistingConnectionHelper}.
    """
    def test_interface(self):
        """
        L{_ExistingConnectionHelper} implements L{_ISSHConnectionCreator}.
        """
        self.assertTrue(
            verifyClass(_ISSHConnectionCreator, _ExistingConnectionHelper))


    def test_secureConnection(self):
        """
        L{_ExistingConnectionHelper.secureConnection} returns a L{Deferred}
        which fires with whatever object was fed to
        L{_ExistingConnectionHelper.__init__}.
        """
        result = object()
        helper = _ExistingConnectionHelper(result)
        self.assertIs(
            result, self.successResultOf(helper.secureConnection()))


    def test_cleanupConnectionNotImmediately(self):
        """
        L{_ExistingConnectionHelper.cleanupConnection} does nothing to the
        existing connection if called with C{immediate} set to C{False}.
        """
        helper = _ExistingConnectionHelper(object())
        # Bit hard to test nothing happens. However, since object() has no
        # relevant methods or attributes, if the code is incorrect we can
        # expect an AttributeError.
        helper.cleanupConnection(object(), False)


    def test_cleanupConnectionImmediately(self):
        """
        L{_ExistingConnectionHelper.cleanupConnection} does nothing to the
        existing connection if called with C{immediate} set to C{True}.
        """
        helper = _ExistingConnectionHelper(object())
        # Bit hard to test nothing happens. However, since object() has no
        # relevant methods or attributes, if the code is incorrect we can
        # expect an AttributeError.
        helper.cleanupConnection(object(), True)



class _PTYPath(object):
    def __init__(self, contents):
        self.contents = contents


    def open(self, mode):
        if mode == "r+":
            return _ReadFile(self.contents)
        raise OSError(ENOSYS, "Function not implemented")



class NewConnectionHelperTests(TestCase):
    """
    Tests for L{_NewConnectionHelper}.
    """
    def test_interface(self):
        """
        L{_NewConnectionHelper} implements L{_ISSHConnectionCreator}.
        """
        self.assertTrue(
            verifyClass(_ISSHConnectionCreator, _NewConnectionHelper))


    def test_defaultPath(self):
        """
        The default I{known_hosts} path is I{~/.ssh/known_hosts}.
        """
        self.assertEqual(
            "~/.ssh/known_hosts", _NewConnectionHelper._KNOWN_HOSTS)


    def test_defaultKnownHosts(self):
        """
        L{_NewConnectionHelper._knownHosts} is used to create a
        L{KnownHostsFile} if one is not passed to the initializer.
        """
        result = object()
        self.patch(_NewConnectionHelper, '_knownHosts', lambda cls: result)

        helper = _NewConnectionHelper(
            None, None, None, None, None, None, None, None, None, None)

        self.assertIs(result, helper.knownHosts)


    def test_readExisting(self):
        """
        Existing entries in the I{known_hosts} file are reflected by the
        L{KnownHostsFile} created by L{_NewConnectionHelper} when none is
        supplied to it.
        """
        key = CommandFactory().publicKeys['ssh-rsa']
        path = FilePath(self.mktemp())
        knownHosts = KnownHostsFile(path)
        knownHosts.addHostKey("127.0.0.1", key)
        knownHosts.save()

        msg("Created known_hosts file at %r" % (path.path,))

        # Unexpand ${HOME} to make sure ~ syntax is respected.
        home = os.path.expanduser("~/")
        default = path.path.replace(home, "~/")
        self.patch(_NewConnectionHelper, "_KNOWN_HOSTS", default)
        msg("Patched _KNOWN_HOSTS with %r" % (default,))

        loaded = _NewConnectionHelper._knownHosts()
        self.assertTrue(loaded.hasHostKey("127.0.0.1", key))


    def test_defaultConsoleUI(self):
        """
        If C{None} is passed for the C{ui} parameter to
        L{_NewConnectionHelper}, a L{ConsoleUI} is used.
        """
        helper = _NewConnectionHelper(
            None, None, None, None, None, None, None, None, None, None)
        self.assertIsInstance(helper.ui, ConsoleUI)


    def test_ttyConsoleUI(self):
        """
        If C{None} is passed for the C{ui} parameter to L{_NewConnectionHelper}
        and /dev/tty is available, the L{ConsoleUI} used is associated with
        /dev/tty.
        """
        tty = _PTYPath(b"yes")
        helper = _NewConnectionHelper(
            None, None, None, None, None, None, None, None, None, None, tty)
        result = self.successResultOf(helper.ui.prompt(b"does this work?"))
        self.assertTrue(result)


    def test_nottyUI(self):
        """
        If C{None} is passed for the C{ui} parameter to L{_NewConnectionHelper}
        and /dev/tty is not available, the L{ConsoleUI} used is associated with
        some file which always produces a C{b"no"} response.
        """
        tty = FilePath(self.mktemp())
        helper = _NewConnectionHelper(
            None, None, None, None, None, None, None, None, None, None, tty)
        result = self.successResultOf(helper.ui.prompt(b"did this break?"))
        self.assertFalse(result)


    def test_defaultTTYFilename(self):
        """
        If not passed the name of a tty in the filesystem,
        L{_NewConnectionHelper} uses C{b"/dev/tty"}.
        """
        helper = _NewConnectionHelper(
            None, None, None, None, None, None, None, None, None, None)
        self.assertEqual(FilePath(b"/dev/tty"), helper.tty)


    def test_cleanupConnectionNotImmediately(self):
        """
        L{_NewConnectionHelper.cleanupConnection} closes the transport cleanly
        if called with C{immediate} set to C{False}.
        """
        helper = _NewConnectionHelper(
            None, None, None, None, None, None, None, None, None, None)
        connection = SSHConnection()
        connection.transport = StringTransport()
        helper.cleanupConnection(connection, False)
        self.assertTrue(connection.transport.disconnecting)


    def test_cleanupConnectionImmediately(self):
        """
        L{_NewConnectionHelper.cleanupConnection} closes the transport with
        C{abortConnection} if called with C{immediate} set to C{True}.
        """
        class Abortable:
            aborted = False
            def abortConnection(self):
                """
                Abort the connection.
                """
                self.aborted = True

        helper = _NewConnectionHelper(
            None, None, None, None, None, None, None, None, None, None)
        connection = SSHConnection()
        connection.transport = SSHClientTransport()
        connection.transport.transport = Abortable()
        helper.cleanupConnection(connection, True)
        self.assertTrue(connection.transport.transport.aborted)
