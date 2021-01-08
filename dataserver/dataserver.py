import zmq
import threading
from datetime import datetime
from queue import Queue
import numpy as np
import random
from tqdm import tqdm
from pathlib import Path

from .parser import parseSensorData, encodeOutput

from ctypes import *

from collections import defaultdict


class DataQueueDict(dict):

    def __init__(self, maxsize):
        self.maxsize = maxsize * 2.0
        super().__init__({})

    def __missing__(self, key):
        res = self[key] = Queue(maxsize = self.maxsize)
        return res


class DataServer():
    
    def __init__(self, sequence_length, num_channels):
        self.sequence_length = sequence_length
        self.num_channels    = num_channels

    def get_batch(self):
        raise NotImplementedError()

    def set_output(self, batch):
        raise NotImplementedError()


class FileServer(DataServer):
    def __init__(self, session_dirs = [], sequence_length = 64, num_channels = 8):
        super().__init__(sequence_length, num_channels)

        session_dirs = [ Path(f) for f in session_dirs]

        self.data  = {}
        self.stats = {}

        recording_index = 0
        for session_dir in session_dirs:

            session = session_dir.stem
            #session_time = datetime.strptime(session[:-4], "%Y-%m-%d-%H-%M-%S")


            suit_data = defaultdict(dict)
            for data_file in session_dir.glob("*.npy"):

                suit     = int(data_file.stem.split('-')[0])
                sample_n = int(data_file.stem.split('-')[1])

                data = np.load(data_file, allow_pickle=True)
                if data.shape[0] > 0 and data.shape[1] == 9:
                    suit_data[suit][sample_n] = data
                else:
                    print(data.shape)
                
            suit_np_data = {}
            for suit, data in suit_data.items():
                data = [ d for k, d in sorted(data.items()) ]

                data = list(data)
                suit_np_data[suit] = np.concatenate(data, axis=0) 

            for suit, data in suit_np_data.items():
                stat = {
                    'index'   : recording_index,
                    'suit'    : suit,
                    'session' : session,
                    'samples' : data.shape[0]
                }

                print( stat )
                self.stats[recording_index] = stat
                self.data[recording_index] = data
                recording_index += 1


        
        print(f"Loaded: {len(self.data)} Sessions")

        # for stat in self.stats:
        #     print(f"{}\t{}\t{}")
        
    def get_batch(self, sequence_length = None):
        sequence = {}

        if sequence_length is None:
            sequence_length + self.sequence_length

        for recording_index, data in self.data.items():
            
            if data.shape[0] > sequence_length:

                srt = random.randint(0, data.shape[0] - sequence_length)
                end = srt + sequence_length

                sequence[recording_index] = data[srt:end,1:]

        return sequence


class ZqmServer(DataServer):

    def __init__(self, ctx, pub_addr, sub_addr, sequence_length = 64, num_channels = 8):
        super().__init__(sequence_length, num_channels)
        self.ctx = ctx
        

        self.buffers = DataQueueDict(maxsize=sequence_length)


        print(f"Connecting to: {pub_addr}")
        self.sub = self.ctx.socket(zmq.SUB)
        self.sub.setsockopt(zmq.SUBSCRIBE, b"")  # Note.
        self.sub.connect(pub_addr)

        print(f"Binding to: {sub_addr}")
        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.bind(sub_addr)


        self.thread = threading.Thread(target=self.receive_loop)
        self.running = True
        self.thread.start()
        
        self._rcv_lock = threading.Lock()
        self._get_lock = threading.Lock()
        self._get_lock.acquire()


    def get_batch(self):#
        sequences = {}

        self._get_lock.acquire()
        for key, buffer in self.buffers.items():

            if( buffer.qsize() > self.sequence_length):
                sequence = []
                for i in range(self.sequence_length):
                    sequence.append(buffer.get())
                    sequences[key] = np.asarray(sequence)

                
                
        self._rcv_lock.release()
        return sequences


    def receive_loop(self):
        while(self.running):

            message = self.sub.recv()
            device, _, data = parseSensorData(message, self.num_channels )
            self._rcv_lock.acquire()
            self.buffers[device].put(data)
            self._get_lock.release()


    def set_output(self, output):
        for device, (loss, embedding) in output.items():

            msg = encodeOutput( device, loss, embedding)
            self.pub.send(msg)