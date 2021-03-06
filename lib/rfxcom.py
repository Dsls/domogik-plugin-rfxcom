# -*- coding: utf-8 -*-

""" This file is part of B{Domogik} project (U{http://www.domogik.org}).

License
=======

B{Domogik} is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

B{Domogik} is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with Domogik. If not, see U{http://www.gnu.org/licenses}.

Plugin purpose
==============

Handle rfxcom usb and the devices it handles

Implements
==========

- Rfxcom
- RfxcomException

@author: Fritz <fritz.smh@gmail.com>
@copyright: (C) 2007-2013 Domogik project
@license: GPL(v3)
@organization: Domogik
"""

import binascii
import traceback
import threading
import time
from Queue import Queue, Empty, Full
import serial as serial
import domogik.tests.common.testserial as testserial

WAIT_BETWEEN_TRIES = 1

RECEIVER_TRANSCEIVER = {
  "0x50" : "310MHz",
  "0x51" : "315MHz",
  "0x52" : "433.92MHz receiver only",
  "0x53" : "433.92MHz transceiver",
  "0x55" : "868.00MHz",
  "0x56" : "868.00MHz FSK",
  "0x57" : "868.30MHz",
  "0x58" : "868.30MHz FSK",
  "0x59" : "868.35MHz",
  "0x5A" : "868.35MHz FSK",
  "0x5B" : "868.95MHz",
}

TYPE_20_MODELS = {
  "0x00" : "X10 security door/window sensor",
  "0x01" : "X10 security motion sensor",
  "0x02" : "X10 security remote (no alive packets)",
  "0x03" : "KD101 (no alive packets)",
  "0x04" : "Visonic PowerCode door/window sensor – primary contact (with alive packets)",
  "0x05" : "Visonic PowerCode motion sensor (with alive packets)",
  "0x06" : "Visonic CodeSecure (no alive packets)",
  "0x07" : "Visonic PowerCode door/window sensor – auxiliary contact (no alive packets)",
  "0x08" : "Meiantech",
  "0x09" : "SA30 (no alive packets)"
}

TYPE_50_MODELS = {
  "0x01" : "THR128/138, THC138",
  "0x02" : "THC238/268,THN132,THWR288,THRN122,THN122,AW129/131",
  "0x03" : "THWR800",
  "0x04" : "RTHN318",
  "0x05" : "La Crosse TX2, TX3, TX4, TX17",
  "0x06" : "TS15C",
  "0x07" : "Viking 02811",
  "0x08" : "La Crosse WS2300",
  "0x09" : "RUBiCSON",
  "0x0A" : "TFA 30.3133"
}

TYPE_52_HUMIDITY_STATUS = {
  "0x00" : "dry",
  "0x01" : "comfort",
  "0x02" : "normal",
  "0x03" : "wet",
}

TYPE_52_MODELS = {
  "0x01" : "THGN122/123, THGN132, THGR122/228/238/268",
  "0x02" : "THGR810, THGN800, THGR810",
  "0x03" : "RTGR328",
  "0x04" : "THGR328",
  "0x05" : "WTGR800",
  "0x06" : "THGR918/928, THGRN228, THGN500",
  "0x07" : "TFA TS34C, Cresta",
  "0x08" : "WT260,WT260H,WT440H,WT450,WT450H",
  "0x09" : "Viking 02035,02038 (02035 has no humidity)",
  "0x0A" : "Rubicson",
}

class RfxcomException(Exception):
    """
    Rfxcom exception
    """

    def __init__(self, value):
        Exception.__init__(self)
        self.value = value

    def __str__(self):
        return repr(self.value)


class Rfxcom:
    """ Rfxcom
    """

    def __init__(self, log, callback, stop, rfxcom_device, cb_device_detected, cb_send_xpl, cb_register_thread, fake_device = None):
        """ Init Disk object
            @param log : log instance
            @param callback : callback
            @param stop : stop Event
            @param rfxcom_device : rfxcom device (ex : /dev/rfxcom)
            @param cb_device_detected : callback to handle detected devices
            @param cb_send_xpl : callback to send a full xpl message
            @param fake_device : fake device. If None, this will not be used. Else, the fake serial device library will be used
        """
        self.log = log
        self.callback = callback
        self.stop = stop

        # fake or real device
        self.fake_device = fake_device
        self.rfxcom_device = rfxcom_device

        self.cb_send_xpl = cb_send_xpl
        self.cb_device_detected = cb_device_detected
        self.cb_register_thread = cb_register_thread

        # serial device
        self.rfxcom = None

        # TODO : how to get proper value ?
        self.seqnbr = 0

        # Queues for writing and receiving packets to/from Rfxcom
        self.write_rfx = Queue()
        self.rfx_response = Queue()

        # Thread to process queue
        write_process = threading.Thread(None,
                                         self.write_daemon,
                                         "write_packets_process",
                                         (),
                                         {})
        self.cb_register_thread(write_process)
        write_process.start()



    def open(self):
        """ Open RFXCOM device

            This is the procedure to start the communication between the application and the RFXCOM transceiver:
            · Send a 14 byte Interface command – Reset:
            packet (hex 0D 00 00 00 00 00 00 00 00 00 00 00 00 00)
            The RFXCOM will now stop the RF receive for 10 seconds. This period is terminated by sending
            a Status Request.
            · Wait at least 50 milliseconds (max 9 seconds) than clear the COM port receive buffers.
            · Send a 14 byte Interface command – Get Status:
            packet (hex 0D 00 00 01 02 00 00 00 00 00 00 00 00 00)
            The RFXCOM will respond with the status and the 10 seconds reset timeout is terminated.
            · If necessary send a select frequency selection command. The 433.92MHz transceiver does not
            have a frequency select and operates always on 433.92MHz.
            The RFXtrx is now ready to receive RF data and to receive commands from the application for the
            transmitter.

        """
        try:
            self.log.info("**** Open RFXCOM ****")
            self.log.info("Try to open RFXCOM : %s" % self.rfxcom_device)
            if self.fake_device != None:
                self.rfxcom = testserial.Serial(self.fake_device, baudrate = 38400, parity = testserial.PARITY_NONE, stopbits = testserial.STOPBITS_ONE, timeout = 5)
            else:
                self.rfxcom = serial.Serial(self.rfxcom_device, baudrate = 38400, parity = serial.PARITY_NONE, stopbits = serial.STOPBITS_ONE, timeout = 5)
            self.log.info("RFXCOM opened")
            self.log.info("**** Set up the RFXCOM ****")
            reset_message = "0D00000000000000000000000000"
            self.log.info("Send 'reset' message : {0}".format(reset_message))
            self.rfxcom.write(binascii.unhexlify(reset_message))
            self.log.info("Wait 2 seconds...")
            time.sleep(2)
            self.log.info("Flush the serial port")
            self.rfxcom.flush()
            get_status_message = "0D00000102000000000000000000"
            self.log.info("Send 'get status' message : {0}".format(get_status_message))
            self.rfxcom.write(binascii.unhexlify(get_status_message))

            # Wait for the status response
            self.log.info("Wait for the status message...")
            # Here is a example of a status response :
            # length : 13
            # data (including length) : 0d010001025315004f6f00000000
            status_msg = self.wait_for(self.stop, 13, "01")
            self.log.info("Status message received : {0}".format(status_msg))
            # decode and display informations about the status
            self.decode_status(status_msg)
          
            # TODO : allow to set some custom setup on startup
            # this need to add some configuration elements
            # one option per protocol (handle only the protocols supported by the plugin and set 0 for others)

            # Init process finished
            self.log.info("RFXCOM is ready to use! Have fun")
        
        except:
            error = "Error while opening RFXCOM : %s. Check if it is the good device or if you have the good permissions on it. Error : %s" % (self.rfxcom_device, traceback.format_exc())
            raise RfxcomException(error)


    def close(self):
        """ close RFXCOM
        """
        self.log.info("Close RFXCOM")
        try:
            self.rfxcom.close()
        except:
            error = "Error while closing device"
            raise RfxcomException(error)
            

    def write_packet(self, data, xpl_trig_message):
        """ Write command to rfxcom
            @param data : command without length
            @param xpl_trig_message : xpl-trig msg to send if success
        """
        # build the packet : <lenght><data>
        length = len(data)/2
        packet = "%02X%s" % (length, data.upper())

        # Put message in write queue
        # we put in queue the sequence number, the built packet and the xpl-trig message to send if the message is successfully write
        seqnbr = gh(packet, 2)
        self.write_rfx.put_nowait({"seqnbr" : seqnbr, 
                                   "packet" : packet,
                                   "xpl_trig_message" : xpl_trig_message})


    def write_daemon(self):
        """ Write packets in queue to RFXCOM and manager errors to resend them
            This function must be launched as a thread in backgroun
 
            How it works (actually solution 2) :

            Solution 1 : 
        
            The sequence number is not used by the transceiver so you can leave it zero if you want. But it can be used in your program to which ACK/NAK message belongs to which transmit message.
            You need to keep the messages in a buffer until they are acknowledged by an ACK. If you got a NAK you have to resend a message.
            For example:
            Transmit message 1
            Transmit message 2
            Transmit message 3
    
            Received ACK 1
            Received NAK 2
            Received ACK 3
    
            Now you know that message number 2 is not correct processed and you send again:
            Transmit message 2
            Received ACK 2

            Solution 2 : 
    
            An easier way is to transmit a message and wait for the acknowledge before you transmit the next command:
            Transmit message 1
            Receive ACK 1
            Transmit message 2
            Receive NAK 2
            Transmit message 2
            Receive ACK 2
            Transmit message 3
            Receive ACK 3
        """
        self.log.info("Start the write_rfx thread")
        # To test, see RFXCOM email from 17/10/2011 at 20:22 
        
        # infinite
        while not self.stop.isSet():

            # Wait for a packet in the queue
            # TODO : avoid to use a blocking call
            try:
                data = self.write_rfx.get(block = True, timeout = 5)
            except Empty:
                continue
            seqnbr = data["seqnbr"]
            packet = data["packet"]
            xpl_trig_message = data["xpl_trig_message"]
            self.log.debug("Get from Queue : %s > %s" % (seqnbr, packet))
            self.rfxcom.write(binascii.unhexlify(packet))

            # TODO : read in queue in which has been stored data readen from rfx
            loop = True
            while loop == True:
                res = self.rfx_response.get(block = True)
                if res["status"] == "NACK":
                    self.debug.warning("Failed to write. Retry in %s : %s > %s" % (WAIT_BETWEEN_TRIES, seqnbr, packet))
                    time.sleep(WAIT_BETWEEN_TRIES)
                    self.rfxcom.write(binascii.unhexlify(packet))
                else:
                    self.log.debug("Command succesfully sent")
                    self.cb_send_xpl(xpl_trig_message)
                    loop = False
            

    def get_seqnbr(self):
        """ Return seqnbr and then increase it
        """
        ret = self.seqnbr
        if ret == 255:
            ret = 0
        else:
            self.seqnbr += 1
        return "%02x" % ret
            

    def wait_for(self, stop, length, msg_type, timeout = None):
        """ Wait for some dedicated message from the Rfxcom. All other received messages will be ignored
        @param stop : an Event to wait for stop request
        @param length : length of the waited message
        @param msg_type : type of the waited message
        @param timeout : timeout : if reached, we raise an error
        """
        self.log.info("Start listening to the rfxcom device for lenght={0}, type={1}".format(length, msg_type))
        try:
            # TODO : handle timeout
            while not stop.isSet():

                self.log.debug("Waiting for a packet from the rfxcom with a length of {0} and hoping type will be {1}...".format(length, msg_type))
                data_len = self.rfxcom.read()
                # handle empty data (why the hell does this happen ?)
                if data_len == '':
                    #self.log.debug("Timeout reached (this is not an issue!!)")
                    continue

                hex_data_len = binascii.hexlify(data_len)
                int_data_len = int(hex_data_len, 16)
                    
                self.log.debug("Packet of length {0} (0x{1}) received, start processing...".format(int_data_len, hex_data_len))
                if int_data_len == length:
                    # We read data
                    data = self.rfxcom.read(int_data_len)
                    hex_data = binascii.hexlify(data)
                    self.log.debug("Packet : %s" % hex_data)
                    the_type = hex_data[0] + hex_data[1]
                    if msg_type == the_type:
                        self.log.debug("Packet type is the one we wait for. End waiting for a dedicated packet")
                        return hex_data
                    else:
                        self.log.debug("Packet type (0x{0})is the one we are waiting for (0x{1}) : skipping this one.".format(the_type, msg_type))
                    
                else:
                    # bad length : skip message
                    # warning : it can besome wrong data so it may be not real data and so we shouldn't wait for 
                    # some data of the given length

                    # TODO : how to handle this ?????
                    self.log.debug("This is not the message we are waiting for")

        except serial.SerialException:
            error = "Error while reading rfxcom device (disconnected ?) : %s" % traceback.format_exc()
            self.log.error(error)
            # TODO : raise for using self.force_leave() in bin ?
            return



    def listen(self, stop):
        """ Start listening to Rfxcom
        @param stop : an Event to wait for stop request
        """
        self.log.info("**** Start really using RFXCOM ****")
        self.log.info("Start listening to the rfxcom device")
        # infinite
        try:
            while not stop.isSet():
                self.read()
        except serial.SerialException:
            error = "Error while reading rfxcom device (disconnected ?) : %s" % traceback.format_exc()
            self.log.error(error)
            # TODO : raise for using self.force_leave() in bin ?
            return

    def read(self):
        """ Read Rfxcom device once
            Wait for a byte. It will give message's length
            Then, read message
        """
        # We wait for a message (and its size)
        #self.log.debug("**** Waiting for a packet from the RFXCOM ****")
        data_len = self.rfxcom.read()

        # if timeout is reached for reading, don't process the rest of the function
        # there is a timeout set in order to allow the plugin to shutdown correctly
        if data_len == "":
            return

        self.log.debug("**** New packet received ****")
        hex_data_len = binascii.hexlify(data_len)
        int_data_len = int(hex_data_len, 16)
        self.log.debug("Packet length = %s" % int_data_len)
        # the max length of a valid message is for :
        # 0x03: Undecoded RF Message
        # it is 36
        if int_data_len > 37:
            self.log.error("It seems that bad data has been received! Length = {0}".format(int_data_len))
            # we skip the next steps in order no to block the plugin
            # but as we skip and don't know what is behind, the next byte will be read as a length and so, some
            # bad data may be processed as valid data and some errors may occurs
            return

        if int_data_len != 0:
            # We read data
            data = self.rfxcom.read(int_data_len)
            hex_data = binascii.hexlify(data)
            self.log.debug("Packet data = %s" % hex_data)

            # Process data
            self._process_received_data(hex_data)


    def _process_received_data(self, data):
        """ Process RFXCOM data
            @param data : data read
        """
        type = data[0] + data[1]
        self.log.debug("Packet type = %s" % type)
        try:
            eval("self._process_%s('%s')" % (type, data))
        except AttributeError:
            warning = "No function for type '%s' with data : '%s'. It may be not yet implemented in the plugin. Full trace : %s" % (type, data, traceback.format_exc())
            self.log.warning(warning)
        except:
            error = "Error while processing type %s : %s" % (type, traceback.format_exc())
            self.log.error(error)


    def decode_status(self, data):
        """ Decode the status message and disply informations about it in the logs
            @param data : status message
        """
        subtype = gh(data, 1)
        seqnbr = gh(data, 2)
        cmnd = gh(data, 3)
        msg1 = ghexa(data, 4)
        msg2 = gh(data, 5)
        msg3 = gb(data, 6)
        msg4 = gb(data, 7)
        msg5 = gb(data, 8)
        msg6 = gh(data, 9)
        msg7 = gh(data, 10)
        msg8 = gh(data, 11)
        msg9 = gh(data, 12)

        # receiver/transceiver type
        self.log.info("- Receiver/transceiver type : {0} - {1}".format(msg1, RECEIVER_TRANSCEIVER[msg1]))
        # firmware version
        self.log.info("- Firmware version : 0x{0} - {1}".format(msg2, int(msg2, 16)))
        # enabled protocoles
        self.log.debug("- Protocol (raw) > msg3 : {0}".format(msg3))
        self.log.info("- Protocol > Enable display of undecoded : {0}".format(get_bit(msg3, 7)))
        self.log.info("- Protocol > RFU6                        : {0}".format(get_bit(msg3, 6)))
        self.log.info("- Protocol > RFU5                        : {0}".format(get_bit(msg3, 5)))
        self.log.info("- Protocol > RSL                         : {0}".format(get_bit(msg3, 4)))
        self.log.info("- Protocol > Lighting4                   : {0}".format(get_bit(msg3, 3)))
        self.log.info("- Protocol > FineOffset/Viking           : {0}".format(get_bit(msg3, 2)))
        self.log.info("- Protocol > Rubicson                    : {0}".format(get_bit(msg3, 1)))
        self.log.info("- Protocol > AE Blyss                    : {0}".format(get_bit(msg3, 0)))

        self.log.debug("- Protocol (raw) > msg4 : {0}".format(msg4))
        self.log.info("- Protocol > BlindsT1                    : {0}".format(get_bit(msg4, 7)))
        self.log.info("- Protocol > BlindsT0                    : {0}".format(get_bit(msg4, 6)))
        self.log.info("- Protocol > ProGuard                    : {0}".format(get_bit(msg4, 5)))
        self.log.info("- Protocol > FS20                        : {0}".format(get_bit(msg4, 4)))
        self.log.info("- Protocol > La Crosse                   : {0}".format(get_bit(msg4, 3)))
        self.log.info("- Protocol > Hideki/UPM                  : {0}".format(get_bit(msg4, 2)))
        self.log.info("- Protocol > AD LightwaveRF              : {0}".format(get_bit(msg4, 1)))
        self.log.info("- Protocol > Mertik                      : {0}".format(get_bit(msg4, 0)))

        self.log.debug("- Protocol (raw) > msg5 : {0}".format(msg5))
        self.log.info("- Protocol > Visonic                     : {0}".format(get_bit(msg5, 7)))
        self.log.info("- Protocol > ATI                         : {0}".format(get_bit(msg5, 6)))
        self.log.info("- Protocol > Oregon Scientific           : {0}".format(get_bit(msg5, 5)))
        self.log.info("- Protocol > Meiantech                   : {0}".format(get_bit(msg5, 4)))
        self.log.info("- Protocol > HomeEasy EU                 : {0}".format(get_bit(msg5, 3)))
        self.log.info("- Protocol > AC                          : {0}".format(get_bit(msg5, 2)))
        self.log.info("- Protocol > ARC                         : {0}".format(get_bit(msg5, 1)))
        self.log.info("- Protocol > X10                         : {0}".format(get_bit(msg5, 0)))


    def _process_20(self, data):
        """ Type 0x20, Security1
        
            Type : command/sensor
            SDK version : 4.12
            Tested : No
        """
        COMMAND = {"00" : "normal",
                   "01" : "normal-delayed",
                   "02" : "alert",
                   "03" : "alert-delayed",
                   "04" : "motion",
                   "05" : "motion-delayed",
                   "06" : "panic",
                   "07" : "end-panic",
                   "08" : "tamper",
                   "09" : "arm-away",
                   "0a" : "arm-away-delayed",
                   "0b" : "arm-home",
                   "0c" : "arm-home-delayed",
                   "0d" : "disarm",
                   # like for the RFXCOM Lan xPL, the lights-on|off command will only command the light1
                   "10" : "lights-off",   # light 1
                   "11" : "lights-on",
                   "12" : "lights-off",   # light 2
                   "13" : "lights-on",
                   "14" : "dark-detected",
                   "15" : "light-detected",
                   "16" : "battery-low",
                   "17" : "pair-kd101",
                   "80" : "normal-tamper",
                   "81" : "normal-delayed-tamper",
                   "82" : "alert-tamper",
                   "83" : "alert-delayed-tamper",
                   "84" : "motion-tamper",
                   "85" : "motion-delayed-tamper",                  }

        options = {}
        subtype = gh(data, 1)
        seqnbr = gh(data, 2)
        id = gh(data, 3,3)
        address = "0x%s" %(id)

        status = COMMAND[gh(data, 6)]
        
        if status[-7:] == "-tamper":
            cmnd = "alert"
            options["tamper"] = "true"
            status = status[:-7]

        if status[-8:] == "-delayed":
            cmnd = status[0:-8]
            options["delay"] = "max"
        else:
            cmnd = status
        if status == "battery-low":
            cmnd = "alert"
            options["low-battery"] = "true"
        if status == "tamper":
            cmnd = "alert"
            options["tamper"] = "true"
  
        battery = int(gh(data, 7)[0], 16) * 10  # percent
        rssi = int(gh(data, 7)[1], 16) * 100/16 # percent
        
        model = "{0}".format(TYPE_20_MODELS["0x{0}".format(subtype)])
        
        self.log.debug("Packet informations :")
        self.log.debug("- type 20 : Security1")
        self.log.debug("- address = {0}".format(address))
        self.log.debug("- command = {0}".format(cmnd))
        self.log.debug("- options = {0}".format(','.join(['%s:%s' % (key, value) for (key, value) in options.items()])))
        self.log.debug("- battery = {0}".format(battery))
        self.log.debug("- rssi = {0}".format(rssi))
        msg = {"device"  : address,
               "command" : cmnd}
        msg.update(options)
        self.cb_send_xpl(schema = "x10.security",
                         data = msg)

        self.cb_send_xpl(schema = "sensor.basic",
                         data = {"device"  : address,
                                 "type"    : "battery",
                                 "current" : battery})

        self.cb_send_xpl(schema = "sensor.basic",
                         data = {"device"  : address,
                                 "type"    : "rssi",
                                 "current" : rssi})

        for feature in ['Security1']:
            self.cb_device_detected(device_type = "rfxcom.security",
                                    type = "xpl_stats",
                                    feature = feature,
                                    data = {"device" : address,
                                            "reference" : model})
        return

    def _process_50(self, data):
        """ Temperature sensors
            Last update : 1.68
        """
        subtype = gh(data, 1)
        seqnbr = gh(data, 2)
        id = gh(data, 3,2)
        address = "temp%s 0x%s" %(subtype[1], id)

        temp_high = gh(data, 5)
        temp_high_bin = gb(data, 5)
        temp_low = gh(data, 6)

        sign = get_bit(temp_high_bin, 7)
        # first bit = 1 => sign = "-"
        if sign == "1":
            # we remove the left bit of temp_high
            temp_high_dec = int(get_bit(temp_high_bin, 6, 7), 2)
            temp = - float((int(temp_low, 16) + 256*temp_high_dec))/10
        # first bit = 0 => sign = "+"
        else:
            temp = float((int(temp_high, 16) * 256 + int(temp_low, 16)))/10

        rssi = int(gh(data, 7)[0], 16) * 100/16 # percent
        battery = (1+int(gh(data, 7)[1], 16)) * 10  # percent

        model = "{0}".format(TYPE_50_MODELS["0x{0}".format(subtype)])

        # debug informations
        self.log.debug("Packet informations :")
        self.log.debug("- type 50 : temperature sensor")
        self.log.debug("- address = {0}".format(address))
        self.log.debug("- model = {0}".format(model))
        self.log.debug("- temperature = {0}".format(temp))
        self.log.debug("- battery = {0}".format(battery))
        self.log.debug("- rssi = {0}".format(rssi))
 
        # send xPL
        self.cb_send_xpl(schema = "sensor.basic",
                         data = {"device" : address,
                                 "type" : "temp",
                                 "current" : temp,
                                 "units" : "c"})
        self.cb_send_xpl(schema = "sensor.basic",
                         data = {"device" : address,
                                 "type" : "battery",
                                 "current" : battery})
        self.cb_send_xpl(schema = "sensor.basic",
                         data = {"device" : address,
                                 "type" : "rssi",
                                 "current" : rssi})

        # handle device features detection
        for feature in ['temperature']:
            self.cb_device_detected(device_type = "rfxcom.temperature", 
                                    type = "xpl_stats",
                                    feature = feature,
                                    data = {"device" : address,
                                            "reference" : model})

    def _process_52(self, data):
        """ Temperature and humidity sensors
            Last update : 1.68
        """
        subtype = gh(data, 1)
        seqnbr = gh(data, 2)
        id = gh(data, 3,2)
        address = "th%s 0x%s" %(subtype[1], id)
        temp_high = gh(data, 5)
        temp_high_bin = gb(data, 5)
        temp_low = gh(data, 6)
        
        sign = get_bit(temp_high_bin, 7)
        # first bit = 1 => sign = "-"
        if sign == "1":
            # we remove the left bit of temp_high
            temp_high_dec = int(get_bit(temp_high_bin, 6, 7), 2)
            temp = - float((int(temp_low, 16) + 256*temp_high_dec))/10
        # first bit = 0 => sign = "+"
        else:
            temp = float((int(temp_high, 16) * 256 + int(temp_low, 16)))/10
            
        humidity = int(gh(data, 7), 16) 
        humidity_status_code = ghexa(data, 8)
        humidity_status = TYPE_52_HUMIDITY_STATUS[humidity_status_code]
        rssi = int(gh(data, 9)[0], 16) * 100/16 # percent
        battery = (1+int(gh(data, 9)[1], 16)) * 10  # percent
 
        model = "{0}".format(TYPE_52_MODELS["0x{0}".format(subtype)])

        # debug informations
        self.log.debug("Packet informations :")
        self.log.debug("- type 52 : temperature and humidity sensor")
        self.log.debug("- address = {0}".format(address))
        self.log.debug("- model = {0}".format(TYPE_52_MODELS["0x{0}".format(subtype)]))
        self.log.debug("- temperature = {0}".format(temp))
        self.log.debug("- humidity = {0}".format(humidity))
        self.log.debug("- humidity status = {0}".format(humidity_status))
        self.log.debug("- battery = {0}".format(battery))
        self.log.debug("- rssi = {0}".format(rssi))

        # send xPL
        self.cb_send_xpl(schema = "sensor.basic",
                         data = {"device" : address, 
                                 "type" : "temp", 
                                 "current" : temp, 
                                 "units" : "c"})
        self.cb_send_xpl(schema = "sensor.basic",
                         data = {"device" : address, 
                                 "type" : "humidity", 
                                 "current" : humidity, 
                                 "description" : humidity_status})
        self.cb_send_xpl(schema = "sensor.basic",
                         data = {"device" : address, 
                                 "type" : "status", 
                                 "current" : humidity_status})
        self.cb_send_xpl(schema = "sensor.basic",
                         data = {"device" : address, 
                                 "type" : "battery", 
                                 "current" : battery})
        self.cb_send_xpl(schema = "sensor.basic",
                         data = {"device" : address, 
                                 "type" : "rssi", 
                                 "current" : rssi})

        # handle device features detection
        for feature in ['temperature', 'humidity']:
            self.cb_device_detected(device_type = "rfxcom.temperature_humidity", 
                                    type = "xpl_stats",
                                    feature = feature,
                                    data = {"device" : address,
                                            "reference" : model})




    
def gh(data, num, len = 1):
    """ Get byte n° <num> from data to byte n° <num + len> in hexadecimal without 0x....
    """
    return data[num*2:(num+(len-1))*2+2]

def ghexa(data, num, len = 1):
    """ Get byte n° <num> from data to byte n° <num + len> in hexadecimal with 0x...
    """
    return "0x" + data[num*2:(num+(len-1))*2+2]

def gb(data, num):
    """ Get byte n° <num> from data to byte n° <num + len> in binary
    """
    x = int(data[num*2:(num)*2+2], 16)
    return ''.join(x & (1 << i) and '1' or '0' for i in range(7,-1,-1)) 

def get_bit(bin_data, num, len = 1):
    """ Get bit n° <num> from bin data to bin n° <num + len>
        Return result in binary : 01010101
    """
    # the 0 is on the right, the 7 is on the left
    num = 7-num
    return bin_data[num:(num+(len-1))+1]

def hexa(bin_data):
    """ Return hexadecimal value for bin data
        This is a shorcut function
    """
    return hex(int(bin_data, 2))
    
