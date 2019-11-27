# Process IEEE 802.15.4 packets and return an unpacked form of them

FRAME_TYPE_BEACON = 0x0
FRAME_TYPE_DATA = 0x1
FRAME_TYPE_ACK = 0x2
FRAME_TYPE_CMD = 0x3

class Packet:

	# Construct an IEEE802154 packet either from individual parts
	# or from a byte stream off the radio passed in as data.
	def __init__(self,
		frame_type = 0,
		src = None,
		dst = None,
		src_pan = None,
		dst_pan = None,
		seq = 0,
		payload = b'',
		ack_req = False,
		data = None
	):
		if data is None:
			self.frame_type = frame_type
			self.ack_req = ack_req
			self.src = src
			self.dst = dst
			self.src_pan = src_pan
			self.dst_pan = dst_pan
			self.seq = seq
			self.payload = payload
		else:
			self.deserialize(data)

	# parse an incoming 802.15.4 message
	def deserialize(self,b):
		j = 0
		fcf = b[j+0] << 0 | b[j+1] << 8
		j += 2
		self.frame_type = (fcf >> 0) & 0x7
		self.ack_req = ((fcf >> 5) & 1) != 0
		dst_mode = (fcf >> 10) & 0x3
		src_mode = (fcf >> 14) & 0x3
		self.src = None
		self.dst = None
		self.dst_pan = None
		self.src_pan = None

		self.seq = b[j]
		j += 1

		if dst_mode != 0:
			# Destination pan is always in the message
			self.dst_pan = (b[j+0] << 0) | (b[j+1] << 8)
			j += 2
			if dst_mode == 2:
				# short destination addresses
				self.dst = b[j+0] << 0 | b[j+1] << 8
				j += 2
			elif dst_mode == 3:
				# long addresses
				self.dst = b[j:j+8]
				j += 8
			else:
				throw("Unknown dst_mode %d" % (dst_mode))

		if src_mode != 0:
			if (fcf >> 6) & 1:
				# pan compression, use the dst_pan
				self.src_pan = self.dst_pan
			else:
				# pan is in the message
				self.src_pan = b[j+0] << 0 | b[j+1] << 8
				j += 2

			if src_mode == 2:
				# short source addressing
				self.src = b[j+0] << 0 | b[j+1] << 8
				j += 2
			elif src_mode == 3:
				# long source addressing
				self.src = b[j:j+8]
				j += 8
			else:
				throw("Unknown src_mode %d" % (src_mode))

		# the rest of the message is the payload for the next layer
		self.payload = b[j:]
		return self

	def serialize(self):
		hdr = bytearray()
		hdr.append(0) # FCF will be filled in later
		hdr.append(0)
		hdr.append(self.seq)

		fcf = self.frame_type & 0x7
		if self.ack_req:
			fcf |= 1 << 5 # Ack request

		# Destination address mode
		hdr.append((self.dst_pan >> 0) & 0xFF)
		hdr.append((self.dst_pan >> 8) & 0xFF)
		if type(self.dst) is int:
			# short addressing, only 16-bits
			fcf |= 0x2 << 10
			hdr.append((self.dst >> 0) & 0xFF)
			hdr.append((self.dst >> 8) & 0xFF)
		elif self.dst is not None:
			# long address, should be 8 bytes
			if len(self.dst) != 8:
				throw("dst address must be 8 bytes")
			fcf |= 0x3 << 10
			hdr.extend(self.dst)
		else:
			# no dst information? this isn't valid?
			pass

		# Source address mode; can be ommitted entirely
		if self.src is not None:
			if self.src_pan is None or self.src_pan == self.dst_pan:
				fcf |= 1 << 6 # Pan ID compression
			else:
				hdr.append((self.src_pan >> 0) & 0xFF)
				hdr.append((self.src_pan >> 8) & 0xFF)

			if type(self.src) is int:
				# short address, only 16-bits
				fcf |= 0x2 << 14
				hdr.append((self.src >> 0) & 0xFF)
				hdr.append((self.src >> 8) & 0xFF)
			else:
				# long address, should be 8 bytes
				if len(self.src) != 8:
					throw("src address must be 8 bytes")
				fcf |= 0x3 << 14
				hdr.extend(self.src)

		# add in the frame control field
		hdr[0] = (fcf >> 0) & 0xFF
		hdr[1] = (fcf >> 8) & 0xFF

		hdr.extend(self.payload)

		return hdr


	# parse an IEEE802.15.4 command
	def cmd_parse(self, b):
		cmd = b.u8()
		if cmd == 0x04:
			self.payload = "Data request"
		elif cmd == 0x07:
			self.payload = "Beacon request"
		else:
			self.payload = "Command %02x" % (cmd)

	# parse the ZigBee network layer
	def nwk_parse(self, b):
		auth_start = b.offset()
		fcf = b.u16()
		dst = b.u16()
		src = b.u16()
		radius = b.u8()
		seq = b.u8()

		frame_type = (fcf >> 0) & 3
		if frame_type == 0x0:
			self.frame_type = "ZDAT"
		elif frame_type == 0x1:
			self.frame_type = "ZCMD"
		elif frame_type == 0x2:
			self.frame_type = "ZRSV"
		elif frame_type == 0x3:
			self.frame_type = "ZPAN"

		if (fcf >> 11) & 1:
			# extended dest is present
			self.dst_addr = b.data(8)
		if (fcf >> 12) & 1:
			# extended source is present
			self.src_addr = b.data(8)

		if fcf & 0x0200:
			# security header is present, attempt to decrypt
			# the message and
			self.ccm_decrypt(b, auth_start)
		else:
			# the rest of the packet is the payload
			self.payload = b.data(b.remaining())

	# security header is present; auth_start points to the start
	# of the network header so that the entire MIC can be computed
	def ccm_decrypt(self, b, auth_start):
		# the security control field is not filled in correctly in the header,
		# so it is necessary to patch it up to contain ZBEE_SEC_ENC_MIC32
		# == 5. Not sure why, but wireshark does it.
		b._data[b.offset()] \
		  = (b._data[b.offset()] & ~0x07) | 0x05
		sec_hdr = b.u8()
		sec_counter = b.data(4)
		# 8 byte ieee address, used in the extended nonce
		if sec_hdr & 0x20: # extended nonce bit
			self.src_addr = b.data(8)
		else:
			# hopefully the one in the header was present
			pass

		# The key seq tells us which key is used;
		# should always be zero?
		key_seq = b.u8()

		# section 4.20 says extended nonce is
		# 8 bytes of IEEE address, little endian
		# 4 bytes of counter, little endian
		# 1 byte of patched security control field
		nonce = bytearray(16)
		nonce[0] = 0x01
		nonce[1:9] = self.src_addr
		nonce[9:13] = sec_counter
		nonce[13] = sec_hdr
		nonce[14] = 0x00
		nonce[15] = 0x00

		# authenticated data is everything up to the message
		# which includes the network header.  we stored the
		# offset to the start of the header before processing it,
		# which allows us to extract all that data now.
		l_a = b.offset() - auth_start
		#print("auth=", auth_start, "b=", b._data)
		auth = b._data[auth_start:auth_start + l_a]
		#print("l_a=",l_a, auth)

		# Length of the message is everything that is left,
		# except the four bytes for the message integrity code
		l_M = 4
		l_m = b.len() - (auth_start + l_a) - l_M
		C = b.data(l_m) # cipher text of length l_m
		M = b.data(l_M) # message integrity code of length l_M

		if not CCM.decrypt(auth, C, M, nonce, self.aes):
			print("BAD DECRYPT: ", b._data )
			print("message=", C)
			self.payload = b''
			self.mic = 0
		else:
			self.payload = C
			self.mic = 1

	def __str__(self):
		return "IEEE802154.Packet(" + ", ".join((
			"dst=" + str(self.dst),
			"dst_pan=" + str(self.dst_pan),
			"src=" + str(self.src), 
			"src_pan=" + str(self.src_pan),
			"seq=" + str(self.seq),
			"frame_type=" + str(self.frame_type),
			"ack_req=" + str(self.ack_req),
			"payload=" + str(self.payload)
		)) + ")"


#from binascii import hexlify
join_test = Packet(
	dst		= 0x0000,
	dst_pan		= 0x1a62,
	src		= b'\x58\xdf\x3e\xfe\xff\x57\xb4\x14',
	src_pan		= 0xFFFF,
	seq		= 123,
	frame_type	= 0x3, # command
	payload		= b'\x01\x80',
	ack_req		= True
)
join_golden = bytearray(b'\x23\xc8\x7b\x62\x1a\x00\x00\xff\xff\x58\xdf\x3e\xfe\xff\x57\xb4\x14\x01\x80')
if join_test.serialize() != join_golden:
	print("serial join test failed:")
	print(join_test)
	print(join_golden)
join_round = Packet(data=join_golden)
if join_round.serialize() != join_golden:
	print("join round trip failed:");
	print(join_round)
	print(join_golden)


resp_test = Packet(
	src		= b'\xb1\x9d\xe8\x0b\x00\x4b\x12\x00',
	dst		= b'\x58\xdf\x3e\xfe\xff\x57\xb4\x14',
	dst_pan		= 0x1a62, # dst_pan
	seq		= 195, # seq
	frame_type	= 0x3, # command
	payload		= b'\x02\x3d\x33\x00',
	ack_req		= True
)
resp_golden = bytearray(b'\x63\xcc\xc3\x62\x1a\x58\xdf\x3e\xfe\xff\x57\xb4\x14\xb1\x9d\xe8\x0b\x00\x4b\x12\x00\x02\x3d\x33\x00')
if resp_test.serialize() != resp_golden:
	print("serial resp test failed:")
	print(resp_test)
	print(resp_golden)
