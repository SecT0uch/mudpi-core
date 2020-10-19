import time
import datetime
import json
import redis
import threading
import sys
import RPi.GPIO as GPIO
from .worker import Worker
sys.path.append('..')

from logger.Logger import Logger, LOG_LEVEL

class RelayWorker(Worker):
	def __init__(self, config, main_thread_running, system_ready, relay_available, relay_active):
		super().__init__(config, main_thread_running, system_ready)
		self.config['pin'] = int(self.config['pin']) #parse possbile strings to avoid errors

		# Events
		self.relay_available = relay_available
		self.relay_active = relay_active

		# Dynamic Properties based on config
		self.active = False
		self.topic = self.config['topic'].replace(" ", "/").lower() if self.config['topic'] is not None else 'mudpi/relay/'
		self.pin_state_off = GPIO.HIGH if self.config['normally_open'] is not None and self.config['normally_open'] else GPIO.LOW
		self.pin_state_on = GPIO.LOW if self.config['normally_open'] is not None and self.config['normally_open'] else GPIO.HIGH

		# Pubsub Listeners
		self.pubsub = self.r.pubsub()
		self.pubsub.subscribe(**{self.topic: self.handleMessage})

		self.init()
		return

	def init(self):
		GPIO.setup(self.config['pin'], GPIO.OUT)
		#Close the relay by default, we use the pin state we determined based on the config at init
		GPIO.output(self.config['pin'], self.pin_state_off)
		time.sleep(0.1)

		#Feature to restore relay state in case of crash  or unexpected shutdown. This will check for last state stored in redis and set relay accordingly
		if(self.config.get('restore_last_known_state', None) is not None and self.config.get('restore_last_known_state', False) is True):
			if(self.r.get(self.config['key']+'_state')):
				GPIO.output(self.config['pin'], self.pin_state_on)
				Logger.log(LOG_LEVEL["info"], 'Restoring Relay \033[1;36m{0} On\033[0;0m'.format(self.config['key']))


		Logger.log(LOG_LEVEL["info"], 'Relay Worker {key}...\t\t\t\033[1;32m Ready\033[0;0m'.format(**self.config))
		return

	def run(self): 
		Logger.log(LOG_LEVEL["info"], 'Relay Worker {key}...\t\t\t\033[1;32m Online\033[0;0m'.format(**self.config))
		return super().run()

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
	
	def turnOn(self):
		#Turn on relay if its available
		if self.relay_available.is_set():
			if not self.active:
				GPIO.output(self.config['pin'], self.pin_state_on)
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
				GPIO.output(self.config['pin'], self.pin_state_off)
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
				except:
					Logger.log(LOG_LEVEL["error"], "Relay Worker \033[1;36m{key}\033[0;0m \t\033[1;31m Unexpected Error\033[0;0m".format(**self.config))

			else:
				#System not ready relay should be off
				self.turnOff()
				time.sleep(1)
				self.resetElapsedTime()
				
			time.sleep(0.1)

		#This is only ran after the main thread is shut down
		#Close the pubsub connection
		self.pubsub.close()
		Logger.log(LOG_LEVEL["info"], "Relay Worker {key} Shutting Down...\t\033[1;32m Complete\033[0;0m".format(**self.config))