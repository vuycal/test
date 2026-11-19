################################################################################
#
#   FileName:   VAR-001 Voltage Control Check
#   Date:       2023-09-27
#       VAR-001:	2021-10-01
#
#  Description: Reports buses that are in/out of their max/min range or
#               desired range as outlined in VAR-001 Voltage and Reactive Control Table 1
#                 
#               The updated table was based on 
#               https://www.aeso.ca/assets/Information-Documents/2010-007RS-General-Operating-Practices-Voltage-Control-2021-10-01.pdf
#				Posted 2021-10-01
#
################################################################################

import sys
import os
#Import PSSPY module for opening and running .sav case
# PSSE_LOCATION = r"C:\Program Files (x86)\PTI\PSSE33\PSSBIN"
# sys.path.append(PSSE_LOCATION)
# os.environ['PATH'] = os.environ['PATH'] + ';' + PSSE_LOCATION
import psse35
import psspy
# psspy.psseinit()
# psspy.case("HIST_2024-02-02_Topology_BaseCase_V35_R1.sav")
class Bus():

	def __init__(self,_substation,_bus,_nominal,_amin,_dmin,_dmax,_amax):
		self.substation = _substation
		self.bus = _bus
		self.nominal = _nominal
		self.abs_min = _amin
		self.des_min = _dmin
		self.des_max = _dmax
		self.abs_max = _amax
		self.exists = True
	
	def get_values(self):
		"""
		Return:
			-1: Bus does not exist
			 0: Bus within range
			 1: Bus outside of desired range
			 2: Bus outside of operating range
		"""
		
		ierr, self.kV = psspy.busdat(self.bus,'KV')
		if ierr > 0:
			self.exists = False
			return -1
		ierr, self.pu = psspy.busdat(self.bus,'PU')
		ierr, self.area = psspy.busint(self.bus,'AREA')
		
		if self.kV > self.abs_max or self.kV < self.abs_min:
			return 2
		
		if self.kV > self.des_max or self.kV < self.des_min:
			return 1
		
		return 0
			
	def report(self):
		
		leftpad = ''
		rightpad = ''
		if 		self.kV < self.abs_min: leftpad = '**'
		elif 	self.kV < self.des_min: leftpad = ' *'
		if 		self.kV > self.abs_max: rightpad = '**'
		elif	self.kV > self.des_max: rightpad = '* '
		
		psspy.report('\n                                         %5i    %-26s    %2i      %5.1f    %5.1f    %2s%5.1f%2s    %5.1f    %5.1f' %(self.bus,self.substation,self.area,self.abs_min,self.des_min,leftpad,self.kV,rightpad,self.des_max,self.abs_max))

class AIES():
		
	def __init__(self,areas):	
		self.areas = areas
		self.buses = []
								#Substation						Bus		kV		Normal Low		Warning Low		Warning High		Normal High
								#--------------------------------------------------------------------------
		#AML
		self.buses.append(Bus(	"5S East Calgary",				162,	240,	240,	242,	254,	255		))
		self.buses.append(Bus(	"5S East Calgary",				600,	138,	136,	138,	142,	142		))
		self.buses.append(Bus(	"13S Deerland",					109,	240,	245,	245,	259,	260		))
		self.buses.append(Bus(	"13S Deerland",					106,	138,	140,	140,	144,	145		))
		self.buses.append(Bus(	"17S Benalto",					155,	240,	240,	246,	256,	260		))
		self.buses.append(Bus(	"17S Benalto",					99,	    138,	134,	138,	144,	145		))
		self.buses.append(Bus(	"19S Wabamun",					134,	138,	136,	140,	144,	145		))
		self.buses.append(Bus(	"28S West Brooks",				430,	240,	242,	245,	260,	264		))
		self.buses.append(Bus(	"28S West Brooks",				276,	138,	137,	138,	144,	145		))
		self.buses.append(Bus(	"37S North Calder",				137,	240,	238,	244,	255,	260		))
		self.buses.append(Bus(	"37S North Calder",				40,	    138,	135,	138,	144,	145		))
		self.buses.append(Bus(	"38S East Edmonton",			136,	240,	239,	240,	253,	255		))
		self.buses.append(Bus(	"38S East Edmonton",			89,	    138,	137,	139,	144,	145		))
		self.buses.append(Bus(	"39S Bickerdike",				90,	    240,	253,	258,	263,	264		))
		self.buses.append(Bus(	"39S Bickerdike",				88,	    138,	137,	140,	144,	145		))
		self.buses.append(Bus(	"42S Sarcee",					161,	240,	240,	247,	253,	253		))
		self.buses.append(Bus(	"42S Sarcee",					202,	138,	137,	138,	142,	142		))
		self.buses.append(Bus(	"58S Edson",					20,	    138,	136,	138,	144,	145		))
		self.buses.append(Bus(	"59S Peigan (TransAlta)",		165,	240,	246,	247,	258,	264		))
		self.buses.append(Bus(	"59S Peigan (TransAlta)",		166,	138,	136,	138,	144,	145		))
		self.buses.append(Bus(	"62S Brazeau",					153,	240,	241,	242,	256,	257		))
		self.buses.append(Bus(	"62S Brazeau",					36,	    138,	138,	140,	144,	145		))
		self.buses.append(Bus(	"63S Red Deer",					152,	240,	238,	245,	256,	257		))
		self.buses.append(Bus(	"63S Red Deer",					117,	138,	136,	138,	144,	145		))
		self.buses.append(Bus(	"64S East Crossfield",			156,	240,	240,	242,	254,	255		))
		self.buses.append(Bus(	"64S East Crossfield",			312,	138,	135,	138,	144,	145		))
		self.buses.append(Bus(	"68S Willesden Green",			150,	240,	244,	245,	256,	264		))
		self.buses.append(Bus(	"69S North Barrhead",			138,	240,	250,	250,	268,	272		))
		self.buses.append(Bus(	"69S North Barrhead",			41,	    138,	140,	140,	144,	145		))
		self.buses.append(Bus(	"71S Lamoureux",				57,	    240,	240,	240,	254,	255		))
		self.buses.append(Bus(	"71S Lamoureux",				63,	    138,	140,	140,	145,	145		))
		self.buses.append(Bus(	"72S Leismer",					348,	240,	248,	262,	271,	280		))
		self.buses.append(Bus(	"72S Leismer",					468,	144,	138,	140,	146,	150		))
		self.buses.append(Bus(	"74S Janet",					160,	240,	240,	248,	254,	264		))
		self.buses.append(Bus(	"74S Janet",					207,	138,	137,	138,	142,	142		))
		self.buses.append(Bus(	"77S Sagitawah",				163,	240,	250,	250,	263,	264		))
		self.buses.append(Bus(	"77S Sagitawah",				164,	138,	140,	140,	144,	145		))
		self.buses.append(Bus(	"83S Taber",					272,	138,	136,	138,	144,	145		))
		self.buses.append(Bus(	"86S Bigstone",					81,	    240,	238,	243,	253,	260		))
		self.buses.append(Bus(	"86S Bigstone",					374,	138,	137,	141,	145,	145		))
		self.buses.append(Bus(	"87S Gaetz",					147,	240,	235,	240,	256,	260		))
		self.buses.append(Bus(	"87S Gaetz",					148,	138,	139,	140,	144,	145		))
		self.buses.append(Bus(	"89S Ellerslie",				258,	500,	513,	520,	540,	550		))
		self.buses.append(Bus(	"89S Ellerslie",				128,	240,	237,	246,	254,	255		))
		self.buses.append(Bus(	"102S Langdon",					159,	240,	240,	251,	255,	258		))
		self.buses.append(Bus(	"118S Canmore",					283,	138,	135,	139,	144,	145		))
		self.buses.append(Bus(	"132S Ware Junction",			225,	240,	244,	254,	262,	264		))
		self.buses.append(Bus(	"157S Lac La Biche",			305,	138,	138,	138,	144,	145		))
		self.buses.append(Bus(	"163S Amoco Empress",			262,	240,	246,	247,	260,	264		))
		self.buses.append(Bus(	"163S Amoco Empress",			267,	138,	138,	140,	144,	145		))
		self.buses.append(Bus(	"244S Bowmanton",				669,	240,	245,	247,	263,	264		))
		self.buses.append(Bus(	"244S Bowmanton",				659,	138,	136,	138,	144,	145		))
		self.buses.append(Bus(	"250P Bighorn",					103,	138,	141,	142,	148,	150		))
		self.buses.append(Bus(	"267S Killarney Lake",			219,	138,	138,	139,	144,	145		))
		self.buses.append(Bus(	"268S Whitecourt",				9,	    138,	138,	138,	144,	145		))
		self.buses.append(Bus(	"275S Jenner",					260,	240,	247,	247,	257,	260		))
		self.buses.append(Bus(	"305S Acheson",					359,	138,	136,	138,	144,	145		))
		self.buses.append(Bus(	"310P Sundance",				135,	240,	249,	255,	262,	264		))
		self.buses.append(Bus(	"310P Sundance",				132,	138,	136,	138,	144,	145		))
		self.buses.append(Bus(	"320P Keephills",				610,	500,	506,	520,	540,	550		))
		self.buses.append(Bus(	"320P Keephills",				421,	240,	239,	250,	258,	260		))
		self.buses.append(Bus(	"320P Keephills",				423,	138,	133,	138,	144,	145		))
		self.buses.append(Bus(	"321S Al Rothbauer",			976,	138,	136,	138,	144,	150		))
		self.buses.append(Bus(	"370S North Lethbridge",		167,	240,	245,	247,	260,	264		))
		self.buses.append(Bus(	"370S North Lethbridge",		692,	138,	136,	138,	144,	145		))
		self.buses.append(Bus(	"377S Hardisty",				73,	    138,	138,	140,	144,	145		))
		self.buses.append(Bus(	"391S Balzac",					198,	138,	137,	138,	144,	145		))
		self.buses.append(Bus(	"394S Empress",					266,	138,	138,	139,	144,	145		))
		self.buses.append(Bus(	"396S Pincher Creek",			224,	138,	136,	140,	144,	145		))
		self.buses.append(Bus(	"520S Bennett",					456,	500,	510,	512,	535,	540		))
		self.buses.append(Bus(	"523S Bullshead",				271,	138,	136,	138,	144,	145		))
		self.buses.append(Bus(	"535S Joffre",					383,	138,	136,	138,	142,	144		))
		self.buses.append(Bus(	"602s Cold Creek",				17,	    138,	135,	138,	144,	145		))
		self.buses.append(Bus(	"648S Metiskow",				87,	    240,	246,	250,	260,	260		))
		self.buses.append(Bus(	"648S Metiskow",				74,	    138,	136,	140,	145,	145		))
		#ATCO
		self.buses.append(Bus(	"700S Bonnyville",				1344,	144,	138,	146,	153,	153		))
		self.buses.append(Bus(	"715S Leming Lake",				1310,	144,	138,	145,	150,	152		))
		self.buses.append(Bus(	"716S Lloydminster",			1455,	144,	142,	143,	150,	151		))
		self.buses.append(Bus(	"722S Crystal Lake",			1121,	144,	143,	144,	152,	154		))
		self.buses.append(Bus(	"730S Rycroft",					1100,	144,	142,	143,	153,	154		))
		self.buses.append(Bus(	"732S Mitsue",					1228,	240,	254,	258,	267,	270		))
		self.buses.append(Bus(	"732S Mitsue",					1229,	144,	145,	146,	153,	154		))
		self.buses.append(Bus(	"740S H.R. Milner",				1147,	144,	144,	145,	151,	152		))
		self.buses.append(Bus(	"743S Sarah Lake",				1266,	144,	142,	144,	151,	152		))
		self.buses.append(Bus(	"749S Flyingshot Lake",			1126,	144,	143,	145,	151,	152		))
		self.buses.append(Bus(	"751S Hill",					1393,	144,	142,	143,	150,	151		))
		self.buses.append(Bus(	"755S Cordel",					1499,	240,	247,	249,	259,	260		))
		self.buses.append(Bus(	"757S Battle River",			1491,	144,	137,	138,	150,	152		))
		self.buses.append(Bus(	"766S Nevis",					1422,	240,	244,	248,	260,	265		))
		self.buses.append(Bus(	"766S Nevis",					1423,	144,	147,	148,	152,	155		))
		self.buses.append(Bus(	"767S Oyen",					1474,	144,	137,	141,	148,	152		))
		self.buses.append(Bus(	"780S Lubicon",					1065,	144,	144,	145,	153,	154		))
		self.buses.append(Bus(	"786S High Level",				1020,	144,	143,	144,	153,	154		))
		self.buses.append(Bus(	"788S Hotchkiss",				1050,	144,	148,	149,	154,	154		))
		self.buses.append(Bus(	"789S Keg River",				1036,	144,	145,	146,	153,	154		))
		self.buses.append(Bus(	"790S Poplar Hill",				1118,	144,	143,	145,	153,	154		))
		self.buses.append(Bus(	"791S Rainbow Lake",			1028,	144,	145,	146,	153,	154		))
		self.buses.append(Bus(	"793S West Peace River",		1085,	144,	147,	148,	153,	154		))
		self.buses.append(Bus(	"800S Friedenstal",				1105,	144,	144,	145,	153,	154		))
		self.buses.append(Bus(	"801S Anderson",				1484,	240,	252,	257,	262,	275		))
		self.buses.append(Bus(	"801S Anderson",				1485,	144,	139,	148,	151,	155		))
		self.buses.append(Bus(	"809S Louise Creek",			1260,	240,	258,	260,	268,	270		))
		self.buses.append(Bus(	"809S Louise Creek",			1261,	144,	143,	144,	151,	152		))
		self.buses.append(Bus(	"811S Clairmont Lake",			1117,	144,	143,	144,	153,	154		))
		self.buses.append(Bus(	"813S Little Smoky",			1163,	240,	259,	261,	269,	270		))
		self.buses.append(Bus(	"813S Little Smoky",			1164,	144,	145,	146,	153,	154		))
		self.buses.append(Bus(	"815S Goodfare",				1119,	144,	142,	144,	153,	154		))
		self.buses.append(Bus(	"825S Whitefish Lake",			1348,	240,	257,	260,	270,	275		))
		self.buses.append(Bus(	"826S Marguerite Lake",			1312,	240,	248,	249,	264,	265		))
		self.buses.append(Bus(	"826S Marguerite Lake",			1313,	144,	139,	141,	153,	154		))
		self.buses.append(Bus(	"827S Cranberry Lake",			1070,	144,	144,	145,	151,	152		))
		self.buses.append(Bus(	"834S Wesley Creek",			1080,	240,	258,	259,	273,	274		))
		self.buses.append(Bus(	"834S Wesley Creek",			1082,	144,	146,	147,	153,	154		))
		#self.buses.append(Bus(	"840S McNeill",					1473,	230,	219,	231,	248,	248		))
		self.buses.append(Bus(	"840S McNeill",					1473,	138,	138,	139,	143,	144		))
		self.buses.append(Bus(	"845S Big Mountain",			1143,	144,	144,	145,	152,	153		))
		self.buses.append(Bus(	"848S Ruth Lake",				1200,	240,	257,	260,	270,	275		))
		self.buses.append(Bus(	"848S Ruth Lake",				1202,	144,	140,	143,	153,	155		))
		self.buses.append(Bus(	"885S McMillian",				204,	240,	252,	260,	274,	275		))
		self.buses.append(Bus(	"885S McMillian",				309,	144,	139,	148,	154,	155		))
		self.buses.append(Bus(	"888S Dover",					666,	240,	257,	260,	271,	275		))
		self.buses.append(Bus(	"898S Heart Lake",				282,	240,	258,	260,	270,	275		))
		self.buses.append(Bus(	"898S Heart Lake",				321,	144,	140,	142,	150,	152		))
		self.buses.append(Bus(	"939s Livock",				    1609,	500,	510,	522,	543,	550		))
		self.buses.append(Bus(	"939s Livock",				    1288,	240,	254,	258,	276,	285		))
		self.buses.append(Bus(	"951s Thickwood",				1601,	500,	516,	522,	544,	550		))
		self.buses.append(Bus(	"951s Thickwood",				1605,	240,	259,	260,	270,	275		))		
		self.buses.append(Bus(	"977S Salt Creek",				1402,	240,	254,	262,	270,	275		))
		self.buses.append(Bus(	"977S Salt Creek",				1405,	144,	143,	145,	153,	155		))
		#ENMAX
		self.buses.append(Bus(	"SS9",							585,	138,	135,	138,	142,	145		))
		self.buses.append(Bus(	"SS13",							559,	138,	135,	138,	142,	145		))
		self.buses.append(Bus(	"SS21",							552,	138,	135,	138,	142,	145		))
		self.buses.append(Bus(	"SS31",							579,	138,	136,	138,	142,	145		))
		self.buses.append(Bus(	"SS41",							592,	138,	136,	138,	142,	145		))
		self.buses.append(Bus(	"SS162 Beddington",				187,	240,	240,	242,	255,	256		))
		self.buses.append(Bus(	"SS162 Beddington",				127,	138,	137,	142,	144,	145		))
		#EPCOR
		self.buses.append(Bus(	"52S East Industrial BusA",		545,	240,	238,	245,	254,	255		))
		self.buses.append(Bus(	"52S East Industrial BusB",		546,	240,	238,	245,	254,	255		))
		self.buses.append(Bus(	"557S Castle Downs",			519,	240,	238,	245,	254,	255		))
		self.buses.append(Bus(	"665S Dome",					512,	240,	239,	245,	254,	255		))
		self.buses.append(Bus(	"803S Lambton",					513,	240,	237,	245,	254,	255		))
		self.buses.append(Bus(	"805S Jasper Terminal",			505,	240,	238,	245,	254,	255		))
		self.buses.append(Bus(	"816S Petrolia",				521,	240,	238,	245,	254,	255		))
		self.buses.append(Bus(	"987S Clover Bar",				514,	240,	238,	245,	254,	255		))
		#self.buses.append(Bus(	"814S Bellamy Terminal",		506,	240,	0,		0,		999,	255		))
		self.buses.append(Bus(	"Genesee 330P",					525,	500,	506,	525,	540,	550		))
		self.buses.append(Bus(	"Genesee 330P",					526,	138,	138,	138,	144,	152		))

	def check_buses(self):
		sorted_buses = {
			-1: [],  # Bus does not exist
			0: [],  # Bus within range
			1: [],  # Bus outside of desired range
			2: []  # Bus outside of operating range
		}

		for bus in self.buses:
			val = bus.get_values()
			if val == -1:  # Bus does not exist
				sorted_buses[-1].append(bus)
			else:
				sorted_buses[val].append(bus)

		return sorted_buses

	def check_case(self, print_all=True):
		#Get results
		sorted_buses = self.check_buses()
		
		if len(sorted_buses[-1]) > 0:
			psspy.report('\n BUSES NOT IN WORKING CASE')
			for bus in sorted_buses[-1]: bus.report()
		
		if len(sorted_buses[0]) > 0 and print_all:
			psspy.report('\n BUSES WITHIN DESIRED RANGE')
			for bus in sorted_buses[0]: bus.report()
		
		if len(sorted_buses[1]) > 0:
			psspy.report('\n BUSES OUTSIDE OF DESIRED RANGE')
			for bus in sorted_buses[1]: bus.report()
		
		if len(sorted_buses[2]) > 0:
			psspy.report('\n BUSES OUTSIDE OF MIN/MAX OPERATING RANGE')
			for bus in sorted_buses[2]: bus.report()
			
		if len(sorted_buses[-1]) == 0 and len(sorted_buses[1]) == 0 and len(sorted_buses[2]) == 0 and (not print_all or len(sorted_buses[0]) == 0):
			psspy.report('\n                                         None')
		
	def check_single_case(self):
		#Create Report
		psspy.report_output(1,'',[0,0])
		self.create_report_header()
		
		self.check_case()
		
	def check_cases_in_folder(self):
		#Find Directory
		dirname = os.path.dirname(os.path.realpath(__file__))
		
		#Create Report
		psspy.report_output(2,os.path.join(dirname,'Results.txt'),[0,0])
		self.create_report_header()
		
		#Find .sav Files in Directory
		caselist = os.listdir(dirname)
		caselist.sort()
		for file in caselist:
			if file.endswith(".sav"):
				#Add title to report
				psspy.report('\n\n\n %s\n '%(file)+'-'*len(file))
				#Open case
				psspy.case(os.path.join(dirname,file))
				
				self.check_case(False)
		
		#Reset Report Output Directory
		psspy.report_output(1,'',[0,0])	
	
	def create_report_header(self):
		
		psspy.beginreport()
		psspy.report(" VOLTAGE VIOLATIONS FOR VAR-001 VOLTAGE CONTROL REPORT\n")
		psspy.report(" =====================================================\n")
		psspy.report(" Checked Buses in Areas: %s\n\n\n\n"%(self.areas))
		psspy.report("                                         Bus #    Substation Name               Area    Normal   Warning    Actual    Warning   Normal \n")
		psspy.report("                                                                                         Low       Low        kV       High      High \n")
		psspy.report("                                         -----    --------------------------    ----    ------   -------   -------   --------   -------")
		
		
	
## Main Function ##
if __name__ == "__main__":
	#areas = [4,6,13,15,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,42,43,44,45,46,47,48,49,52,53,54,55,56,57,60]
	areas = [4,6,13,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,38,39,40,42,43,44,45,46,47,48,49,52,53,54,55,56,57,60]
	#areas = [25, 27, 33, 31, 40, 60]
	aies = AIES(areas)
	aies.check_single_case()
	#aies.check_cases_in_folder()
	

	print('\nComplete!!!!')
		
		
