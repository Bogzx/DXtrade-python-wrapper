# File: ctrader_wrapper.py (Bare Minimum - Step 1, Fixed)

import logging

# --- Twisted ---
# Only import essential Twisted components for basic connection setup
try:
    from twisted.internet import reactor, defer, error as twisted_error
    from twisted.internet.protocol import ReconnectingClientFactory, Protocol
    from twisted.internet.endpoints import SSL4ClientEndpoint
    # <<< FIX: Added import for ClientContextFactory >>>
    from twisted.internet.ssl import ClientContextFactory
    from twisted.internet.defer import inlineCallbacks, returnValue # Keep these for future steps

    TWISTED_AVAILABLE = True
    logging.info("Successfully imported Twisted components.")

except ImportError as e:
    logging.warning(f"Import failed: {e}. Twisted library is required.")
    # Define minimal dummy classes for structure loading if Twisted is missing
    class reactor: # type: ignore
        @staticmethod
        def callLater(delay, func, *args, **kwargs): pass
        @staticmethod
        def stop(): pass
        @staticmethod
        def run(): pass
    class defer: # type: ignore
        @staticmethod
        def Deferred(): return DummyDeferred()
        @staticmethod
        def succeed(result): return DummyDeferred(result)
        @staticmethod
        def fail(failure): return DummyDeferred(failure=failure)
        @staticmethod
        def inlineCallbacks(f): return f # No-op decorator
    class DummyDeferred: # type: ignore
        _result = None; _failure = None
        def addCallback(self, c, *a, **k): return self
        def addErrback(self, e, *a, **k): return self
        def cancel(self): pass
    inlineCallbacks = defer.inlineCallbacks
    def returnValue(val): raise Exception(val) # Simple dummy
    class Protocol: pass
    class ReconnectingClientFactory: clientProtocol = Protocol
    # <<< FIX: Added dummy class >>>
    class ClientContextFactory: pass
    class SSL4ClientEndpoint: pass
    class twisted_error: # type: ignore
        ConnectionRefusedError = ConnectionRefusedError
        TimeoutError = TimeoutError
        ConnectionDone = Exception
        ConnectError = Exception

    TWISTED_AVAILABLE = False

# Configure basic logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Bare Minimum Protocol ---
class CTraderAPIProtocolBare(Protocol):
    """Handles basic connection events (no data processing yet)."""
    factory: 'CTraderClientFactoryBare' # Reference back to the factory

    def connectionMade(self):
        """Called by Twisted when the connection is established."""
        logger.info("Protocol: Connection Established.")
        # Type ignore necessary if factory attribute might not be fully typed yet
        self.factory.clientConnectionMade(self) # type: ignore

    def connectionLost(self, reason):
        """Called by Twisted when the connection is lost."""
        logger.info(f"Protocol: Connection Lost. Reason: {reason.getErrorMessage()}")
        # Factory handles notifying the wrapper
        self.factory.clientConnectionLost(self, reason) # type: ignore

# --- Bare Minimum Factory ---
class CTraderClientFactoryBare(ReconnectingClientFactory):
    """Manages protocol creation and basic connection lifecycle events."""
    protocol = CTraderAPIProtocolBare # Use our bare protocol
    wrapper: 'CTraderWrapperBare' # Reference back to the main wrapper
    _connection_deferred: defer.Deferred | None = None # Deferred for connection result

    # Reconnection settings (can be adjusted later)
    initialDelay = 2
    maxDelay = 30
    factor = 1.5

    def __init__(self, wrapper: 'CTraderWrapperBare'):
        if not TWISTED_AVAILABLE:
            raise ImportError("Twisted library not available.")
        self.wrapper = wrapper

    def buildProtocol(self, addr):
        """Creates an instance of the protocol."""
        logger.info(f"Factory: Building protocol for address {addr}")
        self.resetDelay() # Reset reconnection delay
        p = self.protocol()
        p.factory = self # Provide protocol with reference to factory
        return p

    def clientConnectionMade(self, protocol_instance: CTraderAPIProtocolBare):
        """Callback from protocol when connection is fully established."""
        logger.info("Factory: Connection successful.")
        self.wrapper._set_connection_status(protocol_instance, True)
        # Fire the deferred waiting for connection success
        if self._connection_deferred and not self._connection_deferred.called:
            self._connection_deferred.callback(protocol_instance)
        self._connection_deferred = None

    def clientConnectionLost(self, connector, reason):
        """Called when the connection is lost."""
        logger.warning(f"Factory: Connection lost. Reason: {reason.getErrorMessage()}")
        self.wrapper._set_connection_status(None, False)
        # Use super() to call the parent class method correctly
        super().clientConnectionLost(connector, reason)

    def clientConnectionFailed(self, connector, reason):
        """Called when a connection attempt fails."""
        logger.error(f"Factory: Connection failed. Reason: {reason.getErrorMessage()}")
        self.wrapper._set_connection_status(None, False)
        # Fire the deferred waiting for connection with failure
        if self._connection_deferred and not self._connection_deferred.called:
            self._connection_deferred.errback(reason)
        self._connection_deferred = None
        # Use super() to call the parent class method correctly
        super().clientConnectionFailed(connector, reason)

    def get_connection_deferred(self) -> defer.Deferred:
        """Gets a Deferred that fires with the protocol on success, or errbacks on failure."""
        if self._connection_deferred is None or self._connection_deferred.called:
            # Create a new Deferred, add canceller if needed later
            self._connection_deferred = defer.Deferred()
        return self._connection_deferred


# --- Bare Minimum Wrapper ---
class CTraderWrapperBare:
    """Bare minimum wrapper focusing only on Twisted connection setup."""
    _client: CTraderAPIProtocolBare | None = None # Protocol instance
    _factory: CTraderClientFactoryBare | None = None # Factory instance
    _endpoint = None # Twisted Endpoint instance
    _connector = None # Twisted Connector instance

    _is_connected: bool = False

    def __init__(self, environment: str = "demo"):
        """Initialize basic configuration."""
        if not TWISTED_AVAILABLE:
            raise ImportError("Twisted library not available.")

        if environment not in ["demo", "live"]:
            raise ValueError("Environment must be 'demo' or 'live'")

        # Determine host and port (provide defaults even if SDK Endpoints not imported yet)
        self._host: str = "demo.ctraderapi.com" if environment == "demo" else "live.ctraderapi.com"
        self._port: int = 5035
        self._environment = environment
        logger.info(f"Wrapper initialized for {self._environment} ({self._host}:{self._port})")

    def connect(self) -> defer.Deferred:
        """
        Initiates the connection attempt.

        Returns:
            A Deferred that fires with the Protocol instance on successful connection,
            or errbacks with the failure reason.
        """
        if self._is_connected:
            logger.warning("Already connected.")
            # Ensure client exists if connected
            if self._client:
                 return defer.succeed(self._client)
            else:
                 # Should not happen, indicates inconsistent state
                 logger.error("State inconsistency: connected but no client instance.")
                 return defer.fail(Exception("Inconsistent state: connected but no client"))

        if self._connector:
             logger.warning("Connection attempt already in progress or connector exists.")
             if self._factory:
                  return self._factory.get_connection_deferred()
             else:
                  # This state should ideally not be reachable if logic is correct
                  return defer.fail(Exception("Connecting but factory not available"))

        logger.info(f"Attempting to connect to {self._host}:{self._port}...")

        # Create factory if it doesn't exist
        if self._factory is None:
             self._factory = CTraderClientFactoryBare(self)

        # Create endpoint if it doesn't exist
        if self._endpoint is None:
             # <<< FIX: Create a context factory >>>
             # Use the imported ClientContextFactory
             contextFactory = ClientContextFactory()
             logger.info(f"Creating SSL endpoint for {self._host}:{self._port}...")
             # <<< FIX: Pass contextFactory to the endpoint >>>
             # Type ignore helps if linter struggles with reactor type inference
             self._endpoint = SSL4ClientEndpoint(reactor, self._host, self._port, contextFactory) # type: ignore

        # Get the deferred that will fire upon connection success/failure
        connection_deferred = self._factory.get_connection_deferred()

        # Start connecting. Doesn't block. Stores the connector.
        # Type ignore helps if linter struggles with endpoint type inference
        self._connector = self._endpoint.connect(self._factory) # type: ignore

        # Return the deferred so the caller can wait for the result
        return connection_deferred

    def disconnect(self):
        """Requests disconnection."""
        logger.info("Disconnect requested.")
        # Stop the factory from trying to reconnect automatically
        if self._factory:
            self._factory.stopTrying()
            # Clear the reference to the connection deferred if it exists and hasn't fired
            if self._factory._connection_deferred and not self._factory._connection_deferred.called:
                 try:
                      self._factory._connection_deferred.cancel()
                 except Exception as e:
                      logger.warning(f"Could not cancel connection deferred: {e}")
                 self._factory._connection_deferred = None


        # Ask the connector to disconnect if it exists
        if self._connector:
            try:
                self._connector.disconnect()
                logger.debug("Connector disconnect requested.")
            except Exception as e:
                 logger.error(f"Error during connector.disconnect(): {e}")
        # If connector doesn't exist but client does (already connected), lose connection
        elif self._client and hasattr(self._client, 'transport') and self._client.transport and self._client.transport.connected: # type: ignore
            try:
                 logger.debug("Losing transport connection...")
                 self._client.transport.loseConnection() # type: ignore
            except Exception as e:
                 logger.error(f"Error during transport.loseConnection(): {e}")
        else:
             logger.debug("Disconnect called but no active connector or transport found.")


        # State update (is_connected=False) happens in factory's clientConnectionLost
        # Reset client reference immediately on requesting disconnect
        self._set_connection_status(None, False) # Ensure immediate state update visually
        self._connector = None # Clear connector reference


    def _set_connection_status(self, client_instance: CTraderAPIProtocolBare | None, is_connected: bool):
        """Internal method called by the factory to update state."""
        logger.debug(f"Setting connection status: {is_connected}, client: {client_instance is not None}")
        self._is_connected = is_connected
        self._client = client_instance # Store or clear the protocol instance
        if not is_connected:
            # Perform any cleanup needed when connection drops internally
            self._client = None # Ensure client is None on disconnect


# --- Example Usage ---
if __name__ == "__main__":
    if not TWISTED_AVAILABLE:
        print("Twisted library is required to run this example.")
    else:
        wrapper = CTraderWrapperBare(environment="demo")

        def on_connect_success(protocol_instance):
            print("-" * 30)
            print("CONNECTION SUCCESSFUL! (Protocol instance received)")
            print("Wrapper connected state:", wrapper._is_connected)
            print("Protocol instance:", wrapper._client)
            print("-" * 30)
            print("Example: Disconnecting after 5 seconds.")
            reactor.callLater(5, wrapper.disconnect) # type: ignore
            reactor.callLater(7, reactor.stop) # type: ignore

        def on_connect_failure(failure):
            print("-" * 30)
            print(f"CONNECTION FAILED!")
            # Log the failure reason object itself for more details potentially
            logger.error(f"Connection Failure Details: {failure.value}", exc_info=failure)
            print("-" * 30)
            # Optionally stop the reactor or attempt manual reconnect later
            reactor.callLater(1, reactor.stop) # type: ignore

        # Initiate connection when the reactor starts
        def start_connection():
            print("Requesting connection...")
            connect_deferred = wrapper.connect()
            # Add callbacks to the deferred returned by connect()
            connect_deferred.addCallbacks(on_connect_success, on_connect_failure)

        # Schedule the connection attempt slightly after reactor starts
        reactor.callWhenRunning(start_connection) # type: ignore

        # Start the Twisted reactor
        print("Starting Twisted Reactor...")
        reactor.run() # type: ignore
        print("Twisted Reactor Stopped.")