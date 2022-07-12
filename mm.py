# server/contracting_server.py

from contracting.client import ContractingClient
from contracting.db.encoder import encode, decode
from sanic import Sanic, response
from sanic_cors import CORS
import socketio
import ast
import json
from lamden.crypto.canonical import tx_hash_from_tx
from lamden.crypto.transaction import TransactionException
from lamden.nodes.masternode.webserver import NonceEncoder
from lamden.crypto.wallet import Wallet
from lamden.crypto import transaction
from lamden.storage import BlockStorage, NonceStorage
from lamden import storage
from lamden.nodes.delegate import execution
from lamden.logger.base import get_logger
from contracting.db.driver import ContractDriver
from contracting.execution.executor import Executor

log = get_logger("MN-WebServer")

client = ContractingClient()
db = BlockStorage()
nonces = NonceStorage()
driver = ContractDriver()
mn_wallet = Wallet('a'*64)
sio = socketio.AsyncClient()
event_service_port =8080

topics = ["new_block"]
ws_clients = set()

###Put your contract imports here. Some examples below

with open(".path/to/currency.s.py") as f:
	contract = f.read()
	client.submit(contract, 'currency', constructor_args={
                          "vk": "<some wallet address>"})
                          
with open('.path/to/stamp_cost.s.py') as f:
    code = f.read()
    client.submit(code, name='stamp_cost')
    

app = Sanic("Minified_Mainnet")
CORS(app)
                      
def __setup_sio_event_handlers():
    @sio.event
    async def connect():
        print("CONNECTED TO EVENT SERVER")
        for topic in topics:
            await sio.emit('join', {'room': topic})

    @sio.event
    async def disconnect():
        print("DISCONNECTED FROM EVENT SERVER")
        for topic in topics:
            await sio.emit('leave', {'room': topic})

    @sio.event
    async def event(data):
        for client in ws_clients:
            await client.send(json.dumps(data, cls=NonceEncoder))
            
def __register_app_listeners():
    @app.listener('after_server_start')
    async def connect_to_event_service(app, loop):
        # TODO(nikita): what do we do in case event service is not running?
        try:
            await sio.connect(f'http://localhost:{event_service_port}')
            await sio.wait()
        except Exception as err:
            print(err)

#websocket 
@app.websocket('/')
async def ws_handler(request, ws):
    ws_clients.add(ws)

    try:
        driver.clear_pending_state()

        # send the connecting socket the latest block
        num = storage.get_latest_block_height(driver)
        
        #print(num)
        
        block = db.get_block(int(num))
        
        #print(block)

        eventData = {
            'event': 'latest_block',
            'data': block
        }

        await ws.send(json.dumps(eventData, cls=NonceEncoder))

        # async for message in ws:
        #     pass
    finally:
        ws_clients.remove(ws)


# Make sure the server is online
@app.route("/ping")
async def ping(request):
    return response.json({'status': 'online'})


# Get all Contracts in State (list of names)
@app.route("/contracts")
async def get_contracts(request):
    contracts = client.get_contracts()
    return response.json({'contracts': contracts})


@app.route("/contracts/<contract>")
# Get the source code of a specific contract
async def get_contract(request, contract):
    # Use the client raw_driver to get the contract code from the db
    contract_code = client.raw_driver.get_contract(contract)

    # Return an error response if the code does not exist
    if contract_code is None:
        return response.json({'error': '{} does not exist'.format(contract)}, status=404)

    funcs = []
    variables = []
    hashes = []

    # Parse the code into a walkable tree
    tree = ast.parse(contract_code)

    # Parse out all functions
    function_defs = [n for n in ast.walk(
        tree) if isinstance(n, ast.FunctionDef)]
    for definition in function_defs:
        func_name = definition.name
        kwargs = [arg.arg for arg in definition.args.args]

        funcs.append({'name': func_name, 'arguments': kwargs})

    # Parse out all defined state Variables and Hashes
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    for assign in assigns:
        if type(assign.value) == ast.Call:
            if assign.value.func == 'Variable':
                variables.append(assign.targets[0].id.lstrip('__'))
            elif assign.value.func == 'Hash':
                hashes.append(assign.targets[0].id.lstrip('__'))

    # Return all Information
    return response.json({
        'name': contract,
        'code': contract_code,
        'methods': funcs,
        'variables': variables,
        'hashes': hashes
    }, status=200)

# Return the current state of a variable


@app.route("/contracts/<contract>/<variable>")
async def get_variable(request, contract, variable):
    # Check if contract exists. If not, return error
    contract_code = client.raw_driver.get_contract(contract)
    if contract_code is None:
        return response.json({'error': '{} does not exist'.format(contract)}, status=404)
    # Parse key from request object
    key = request.args.get('key')
    if key is not None:
        key = key.split(',')

    # Create the key contracting will use to get the value
    k = client.raw_driver.make_key(
        contract=contract, variable=variable, args=key)

    # Get value
    value = client.raw_driver.get(k)

    # If the variable or the value didn't exists return None
    if value is None:
        return response.json({'value': None}, status=404)

    # If there was a value, return it formatted
    return response.json({'value': value}, status=200, dumps=encode)
    
@app.route("/nonce/<vk>", methods=["GET"])    
async def get_nonce(request, vk):
    latest_nonce = nonces.get_latest_nonce(sender=vk, processor=mn_wallet.verifying_key)

    try:
        latest_nonce = int(latest_nonce)
    except:
        pass

    return response.json({
        'nonce': latest_nonce,
        'processor': mn_wallet.verifying_key,
        'sender': vk})


@app.route("/", methods=["POST", ])
async def submit_transaction(request):
		
	log.debug(f'New request: {request}')
	
	tx_raw = json.loads(request.body)
	log.debug(tx_raw)
	# Check that the payload is valid JSON
	tx = decode(request.body)
	
	if tx is None:
		return response.json({'error': 'Malformed request body.'}, status=404)
		
	try:
            nonce, pending_nonce = transaction.get_nonces(
                sender=tx['payload']['sender'],
                processor=tx['payload']['processor'],
                driver=nonces
            )

            pending_nonce = transaction.get_new_pending_nonce(
                tx_nonce=tx['payload']['nonce'],
                nonce=nonce,
                pending_nonce=pending_nonce
            )

            nonces.set_pending_nonce(
                sender=tx['payload']['sender'],
                processor=tx['payload']['processor'],
                value=pending_nonce
	
	    )
	
	except TransactionException as e:
            log.error(f'Tx has error: {type(e)}')
            log.error(tx)
            return response.json(
                transaction.EXCEPTION_MAP[type(e)]
            )
		
	# process transaction and store result
	process_txn(request.body)
	
	# Return the TX hash to the user so they can track it
	tx_hash = tx_hash_from_tx(tx)
	
	
	return response.json({
		'success': 'Transaction successfully submitted to the network.',
		'hash': tx_hash})


def process_txn(tx):

    e = execution.SerialExecutor(executor=client.executor)

    blck = e.execute_tx(decode(tx), stamp_cost=20_000)
    
    h = storage.get_latest_block_height(driver)
    num = h + 1
    driver.clear_pending_state()
    storage.set_latest_block_height(num, driver)
    

    block = {
    	"number": num,
        "subblocks": [
            {
                "subblock": 0,
                "transactions": [blck]
            }
        ]
    }
    
    #block["number"] += 1
        
    db.store_block(block)
    
    #db.put(block, BlockStorage.TX)
    
@app.route('/latest_block', methods=['GET', 'OPTIONS', ])   
async def get_latest_block(request):
    driver.clear_pending_state()

    num = storage.get_latest_block_height(driver)
    block = db.get_block(int(num))
    return response.json(block, dumps=NonceEncoder().encode)

@app.route('/latest_block_num', methods=['GET']) 
async def get_latest_block_number(request):
    driver.clear_pending_state()

    num = storage.get_latest_block_height(driver)

    return response.json({'latest_block_number': num})

@app.route('/latest_block_hash', methods=['GET']) 
async def get_latest_block_hash(request):
     driver.clear_pending_state()

     return response.json({'latest_block_hash': storage.get_latest_block_hash(driver)})
     
@app.route('/blocks', methods=['GET']) 
async def get_block(request):
    num = request.args.get('num')
    _hash = request.args.get('hash')

    if num is not None:
         block = db.get_block(int(num))
    elif _hash is not None:
         block = db.get_block(_hash)
    else:
         return response.json({'error': 'No number or hash provided.'}, status=400)

    if block is None:
         return response.json({'error': 'Block not found.'}, status=400)

    return response.json(block, dumps=NonceEncoder().encode)


@app.route("/tx", methods=["GET"])
async def get_tx(request):
    _hash = request.args.get('hash')

    if _hash is not None:
        try:
            int(_hash, 16)
            tx = db.get_tx(_hash)
        except ValueError:
            return response.json({'error': 'Malformed hash.'}, status=400)
    else:
        return response.json({'error': 'No tx hash provided.'}, status=400)

    if tx is None:
        return response.json({'error': 'Transaction not found.'}, status=400)

    return response.json(tx, dumps=NonceEncoder().encode)


if __name__ == "__main__":
    __setup_sio_event_handlers()
    __register_app_listeners()
    app.run(host="0.0.0.0", port=3737)
