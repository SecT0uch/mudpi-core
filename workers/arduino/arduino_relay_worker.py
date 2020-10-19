import time
import datetime
import json
import redis
import threading
import sys
import socket
from nanpy import (SerialManager, ArduinoApi)
from nanpy.serialmanager import SerialManagerError
from nanpy.sockconnection import (SocketManager, SocketManagerError)
from .worker import Worker
sys.path.append('..')

import variables
from logger.Logger import Logger, LOG_LEVEL

class ArduinoRelayWorker(Worker):
	def __init__(self, config, main_thread_running, system_ready, relay_available, relay_active, node_connected, connection=None, api=None):
		super().__init__(config, main_thread_running, system_ready)
		self.config['pin'] = int(self.config['pin']) # parse possbile strings to avoid errors

		# Events
		self.main_thread_running = main_thread_running
		self.system_ready = system_ready
		self.relay_available = relay_available
		self.relay_active = relay_active
		self.node_connected = node_connected

		# Dynamic Properties based on config
		self.active = False
		self.relay_ready = False
		self.topic = self.config['topic'].replace(" ", "/").lower() if self.config['topic'] is not None else 'mudpi/relay/*'

		# Pubsub Listeners
		self.pubsub = self.r.pubsub()
		self.pubsub.subscribe(**{self.topic: self.handleMessage})
		self.api = api

		if self.node_connected.is_set():
			self.init()
		return

	def init(self):
		Logger.log(LOG_LEVEL["info"], '{name} Relay Worker {key}...\t\t\033[1;32m Initializing\033[0;0m'.format(**self.config))
		self.api = self.api if self.api is not None else ArduinoApi(connection)
		self.pin_state_off = self.api.HIGH if self.config['normally_open'] is not None and self.config['normally_open'] else self.api.LOW
		self.pin_state_on = self.api.LOW if self.config['normally_open'] is not None and self.config['normally_open'] else self.api.HIGH
		self.api.pinMode(self.config['pin'], self.api.OUTPUT)
		#Close the relay by default, we use the pin state we determined based on the config at init
		self.api.digitalWrite(self.config['pin'], self.pin_state_off)
		time.sleep(0.1)

		#Feature to restore relay state in case of crash  or unexpected shutdown. This will check for last state stored in redis and set relay accordingly
		if(self.config.get('restore_last_known_state', None) is not None and self.config.get('restore_last_known_state', False) is True):
			if(self.r.get(self.config['key']+'_state')):
				self.api.digitalWrite(self.config['pin'], self.pin_state_on)
				Logger.log(LOG_LEVEL["warning"], 'Restoring Relay \033[1;36m{0} On\033[0;0m'.format(self.config['key']))

		self.relay_ready = True
		return

	def run(self): 
		t = threading.Thread(target=self.work, args=())
		t.start()
		Logger.log(LOG_LEVEL["info"], 'Node Relay {key} Worker...\t\t\033[1;32m Online\033[0;0m'.format(**self.config))
		return t

	def decodeMessageData(self, message):
		if isinstance(message, dict):
			#print('Dict Found')
			return message
		elif isinstance(message.decode('utf-8'), str):
			try:
				temp = json.loads(message.decode('utf-8'))
				#print('Json Found')
				return temp
			except:
				#print('Json Error. Str Found')
				return {'event':'Unknown', 'data':message}
		else:
			#print('Failed to detect type')
			return {'event':'Unknown', 'data':message}

	def handleMessage(self, message):
		data = message['data']
		if data is not None:
			decoded_message = self.decodeMessageData(data)
			try:
				if decoded_message['event'] == 'Switch':
					if decoded_message.get('data', None):
						self.relay_active.set()
					elif decoded_message.get('data', None) == 0:
						self.relay_active.clear()
					Logger.log(LOG_LEVEL["info"], 'Switch Relay \033[1;36m{0}\033[0;0m state to \033[1;36m{1}\033[0;0m'.format(self.config['key'], decoded_message['data']))
				elif decoded_message['event'] == 'Toggle':
					state = 'Off' if self.active else 'On'
					if self.relay_active.is_set():
						self.relay_active.clear()
					else:
						self.relay_active.set()
					Logger.log(LOG_LEVEL["info"], 'Toggle Relay \033[1;36m{0} {1} \033[0;0m'.format(self.config['key'], state))
			except:
				Logger.log(LOG_LEVEL["error"], 'Error Decoding Message for Relay {0}'.format(self.config['key']))

	def elapsedTime(self):
		self.time_elapsed = time.perf_counter() - self.time_start
		return self.time_elapsed

	def resetElapsedTime(self):
		self.time_start = time.perf_counter()
		pass
	
	def turnOn(self):
		#Turn on relay if its available
		if self.relay_available.is_set():
			if not self.active:
				self.api.digitalWrite(self.config['pin'], self.pin_state_on)
				message = {'event':'StateChanged', 'data':1}
				self.r.set(self.config['key']+'_state', 1)
				self.r.publish(self.topic, json.dumps(message))
				self.active = True
				#self.relay_active.set() This is handled by the redis listener now
				self.resetElapsedTime()	

	def turnOff(self):
		#Turn off volkeye to flip off relay
		if self.relay_available.is_set():
			if self.active:
				self.api.digitalWrite(self.config['pin'], self.pin_state_off)
				message = {'event':'StateChanged', 'data':0}
				self.r.delete(self.config['key']+'_state')
				self.r.publish(self.topic, json.dumps(message))
				#self.relay_active.clear() This is handled by the redis listener now
				self.active = False
				self.resetElapsedTime()

	def work(self):
		self.resetElapsedTime()
		while self.main_thread_running.is_set():
			if self.system_ready.is_set():
				if self.node_connected.is_set():
					if self.relay_ready:
						try:
							self.pubsub.get_message()
							if self.relay_available.is_set():
								if self.relay_active.is_set():
									self.turnOn()
								else:
									self.turnOff()
							else:
								self.turnOff()
								time.sleep(1)
						except e:
							Logger.log(LOG_LEVEL["error"], "Node Relay Worker \033[1;36m{key}\033[0;0m \t\033[1;31m Unexpected Error\033[0;0m".format(**self.config))
							Logger.log(LOG_LEVEL["error"], "Exception: {0}".format(e))
					else:
						self.init()
				else: 
					# Node offline
					self.relay_ready = False
					time.sleep(5)

			else:
				#System not ready relay should be off
				self.turnOff()
				time.sleep(1)
				self.resetElapsedTime()
				
			time.sleep(0.1)


		#This is only ran after the main thread is shut down
		#Close the pubsub connection
		self.pubsub.close()
		Logger.log(LOG_LEVEL["info"], "Node Relay {key} Shutting Down...\t\033[1;32m Complete\033[0;0m".format(**self.config))