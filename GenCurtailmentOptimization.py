import subprocess
import pandas as pd
import ast
import numpy as np
import log_standAlone as log
from datetime import datetime
import os, sys
import case_standAlone
from ShiftFactor_StandAlone import ShiftFactor
from scipy.optimize import linprog as lp
from io import StringIO
ALMOSTZERO = 0.01

# specify the path to your Python 2.7 interpreter

# Append system path for PSSE and Python API

PSSBIN_PATH = r"C:\Program Files\PTI\PSSE35\35.4\PSSBIN"
PSSE_BUSES = 50000


PSSECMD_EXE = os.path.join(PSSBIN_PATH, "pssecmd35.exe")
INSIDE_PSSECMD = os.environ.get("RUN_INSIDE_PSSECMD") == "1"


def run_in_pssecmd(script):
    script = os.path.abspath(script)
    bootstrap = (
        "import os, sys, traceback\n"
        "code = 0\n"
        "try:\n"
        f"    exec(compile(open({script!r}).read(), {script!r}, 'exec'),"
        f" {{'__name__': '__main__', '__file__': {script!r}}})\n"
        "except BaseException:\n"
        "    traceback.print_exc()\n"
        "    code = 1\n"
        "sys.stdout.flush()\n"
        "sys.stderr.flush()\n"
        "os._exit(code)\n"
    )
    commands = "PYTHON\nexec(%r)\n" % bootstrap
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        [PSSECMD_EXE, "-buses", str(PSSE_BUSES)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        env=dict(os.environ, RUN_INSIDE_PSSECMD="1"),
        **kwargs,
    )
    proc.stdin.write(commands)
    proc.stdin.close()
    for line in proc.stdout:
        print(line, end="", flush=True)
    return proc.wait()


if __name__ == "__main__" and not INSIDE_PSSECMD:
    sys.exit(run_in_pssecmd(__file__))

import psspy
import pssarrays

Albregions = {'Calgary':[57,6],
'CentralEast':[42, 36, 37, 32, 13, 56, 28],
'CentralWest':[30 ,35, 34, 38, 39],
'Edmonton':[40, 60, 31],
'NorthWest':[17, 18, 19, 20, 21, 23, 26, 24, 22],
'NorthEast':[25, 27, 33],
'SouthEast':[43, 48, 47, 52, 54, 4],
'SouthWest':[44, 45, 46, 49, 53, 55]}
class Gen:
    def __init__(self, MW, index, areas):
        self.MW = MW
        self.index = index
        self.opposingAreas = areas
def getGenData(busData, busnums):
    psspy.bsys(0,0,[ 0.385, 500.],0,[],len(busnums),busnums,0,[],0,[])
    ierr, genBusNumber = psspy.amachint(0, 1, 'NUMBER')
    genArea = []
    for genBus in genBusNumber[0]:
        area = [busdata[2] for busdata in busData if busdata[0] == genBus]
        genArea.append(area[0])
        
    ierr, genID = psspy.amachchar(0, 1, 'ID')
    ierr, genMW = psspy.amachreal(0, 1, 'PGEN')
    genDataDict = {}

    for bus, id, mw, genArea in zip(genBusNumber[0], genID[0], genMW[0], genArea):
        if mw > 0:
            if genArea not in genDataDict:
                genDataDict[genArea] = {}
            genDataDict[genArea][(bus, id)] = Gen(mw, 0, [])  
    psspy.bsysdef(0,0)

    return genArea, genDataDict
def curtail(workingcasePath, newWorkingCase, workingCase, saveCase, areas_toMonitor, areas_toCuratil, inputDF, studyRegion, curtail_thermal, PreProjectGen_df, MonitoredLines_df, logFile):    
    psspy.psseinit(16000) 
    _i=psspy.getdefaultint()
    _f=psspy.getdefaultreal()
    _s=psspy.getdefaultchar()
    progreeReport = os.path.join(workingcasePath,saveCase.replace(".sav","Progress.txt"))
    psspy.report_output(2,progreeReport,[2,0])
    logFile.info(f"The PSS\E progress will be available in {progreeReport}.")
    psspy.case(os.path.join(workingcasePath,newWorkingCase))      
    psspy.fnsl([0,0,0,0,2,0,0,0])
    psspy.bsys(0,1,[ 69.0, 500.],len(areas_toMonitor),areas_toMonitor,0,[],0,[],0,[])
    ierr, busV = psspy.abusreal(-1, 1, 'BASE')
    ierr, busNum = psspy.abusint(-1, 1, ['NUMBER','AREA'])
    busData = zip(busNum[0], busV[0], busNum[1])
    busData = list(busData)
    OpposingAreas = {4:[17,18,25,29,30],52:[17,18,25,29,30],48:[17,18,25,29,30],47:[17,18,25,29,30],43:[17,18,25,29,30],45:[17,18,25,29,30],49:[17,18,25,29,30],54:[17,18,25,29,30],55:[17,18,25,29,30],53:[17,18,25,29,30],46:[17,18,25,29,30],44:[17,18,25,29,30], 57:[17,18,25,29,30], 6:[17,18,25,29,30],40:[4,55,52,48,53], 60:[4,55,52,48,53], 31:[4,55,52,48,53],28:[22,20,17,18,29], 56:[22,20,17,18,25], 13:[22,20,17,18,25], 32:[22,20,17,18,25], 37:[22,20,17], 36:[22,20,17,18,25], 42:[22,20,17,18,25], 35:[22,20,17,18,25,27], 39:[22,20,17,18,25,27], 29:[25, 17, 18, 4, 48], 30:[ 17, 18, 4, 48], 38:[ 17, 18, 4, 48], 34:[ 17, 18, 4, 48], 25:[4,48,52,49,46], 27:[4,48,52,49,46], 33:[4,48,52,49,46], 17:[4,48,52,49,46], 18:[4,48,52,49,46], 19:[4,48,52,49,46], 20:[4,48,52,49,46], 21:[4,48,52,49,46], 22:[4,48,52,49,46], 23:[4,48,52,49,46], 24:[4,48,52,49,46], 26:[4,48,52,49,46]}
    OpposingRegions  = {"CentralEast":[17, 18, 19, 20,23],"CentralWest":[25,27], "Edmonton":[25,17,18] , "NorthWest":[48,4], "NorthEast":[53,46,55], "SouthEast":[25,27,18,17], "SouthWest":[25,27,18,17]}
    if "WP" in workingCase:
        ierr, powerflow = psspy.aflowreal(0,1, 6,1,['P', 'RATEB','MVA'])
    else:
        ierr, powerflow = psspy.aflowreal(0,1, 6,1,['P', 'RATEA','MVA'])
    bnb = []
    try:
        # buses which have machines that should not be curtailed at all
        df = inputDF.dropna(subset=['Bus'])
        # Filter rows where 'sheet_name' is 'PrescribedDispatch' and select the 'Bus' column
        bnb = df[df['sheet_name'] == 'PrescribedDispatch']['Bus'].unique().tolist()

        # Convert the bus numbers in the list to integers
        bnb = [int(x) for x in bnb]
    except:
        pass

    if curtail_thermal:
        logFile.info("BIG THERMAL UNITS WILL BE CURTAILED.")
    else:
        logFile.info("BIG THERMAL UNITS WILL NOT BE CURTAILED.")
        PreProjectGen_df = PreProjectGen_df.loc[PreProjectGen_df['CC, COGEN, CtG?']=="No"]

    PreProjectGen_df = PreProjectGen_df.loc[PreProjectGen_df['Area'].isin(areas_toCuratil)]
    ierr, brnint = psspy.aflowint(0, 1, 6, 1, ["FROMNUMBER", "TONUMBER"])
    ierr, brnchar = psspy.aflowchar(0, 1, 6, 1, ["ID"])
    psspy.bsysdef(0,0)
    busKV = []
    for fromBus in brnint[0]:
        buskv = [busdata[1] for busdata in busData if busdata[0] == fromBus]
        busKV.append(buskv[0])
    PF = [abs(powerflow[0][i] / powerflow[2][i]) if powerflow[2][i] > 0 else 0 for i in range(len(powerflow[0]))]

    brndata = zip(brnint[0], brnint[1], brnchar[0], powerflow[0],powerflow[1], PF,busKV)
    brndata = list(brndata)

    gen_bus = [int(x) for x in np.unique(PreProjectGen_df['Bus'].tolist())]
    psspy.bsys(0,0,[  0.385, 500.],0,[],len(gen_bus),gen_bus,0,[],0,[])
    ierr, busV = psspy.abusreal(0, 1, 'BASE')
    ierr, busNum = psspy.abusint(0, 1, ['NUMBER','AREA'])
    psspy.bsysdef(0,0)

    busData = zip(busNum[0], busV[0], busNum[1])
    busData = list(busData)



 
    
    genArea, genDataDict = getGenData(busData,gen_bus)


    lb = []
    ub = []
    c = []
    gens_to_exclude = []
    for genArea, genDictinArea in genDataDict.items():
        logFile.info(f'''{genArea}''')
        
        
        for (bus, id), gen_obj in genDictinArea.items():
            
            if bus not in bnb:
                if genDictinArea[(bus, id)].MW<0:
                    del genDictinArea[(bus, id)]
                    # print(genBusNumber)
                else:
                    lb.append(0)
                    ub.append(genDictinArea[(bus, id)].MW)
                    genDictinArea[(bus, id)].index = len(c)
                    if genArea not in [15]: # if it is in intertie we do not need to adjust the generation so there is no opposing generation 
                        if genArea in OpposingAreas.keys():
                            if bus==3286:
                                print('yes')
                            genDictinArea[(bus, id)].opposingAreas = list(set(OpposingAreas[genArea])-set(areas_toCuratil))
                            
                        else:
                            genDictinArea[(bus, id)].opposingAreas = [15]
                    c.append(1)
            else:
                gens_to_exclude.append((genArea,(bus, id)))
    
    for key in gens_to_exclude:
        del genDataDict[key[0]][key[1]]
    

    numGens = len(c)


    b = []
    genEff_dict = {(row['fromBus'], row['toBus'], row['branchId'], row['injectionBus']): row['genEffectivness']
                for _, row in genEff_df.iterrows()}
    
    count = 0
    A_row = []
    A_col = []
    data = []
    row = 0
    slack_count  = 0
    if len(MonitoredLines_df)>0:
        MonitoredLines = MonitoredLines_df['BranchID']

        if len(MonitoredLines)>0:
            df = pd.DataFrame(brndata, columns=['fromBus', 'toBus', 'ID', 'flow', 'nominal_flow','PF','kV'])
            filtered_list = []
            for line in MonitoredLines:
                print(line)
                line = line.replace("Br_","").split("_")
                mon_branch_df = df.loc[(df['fromBus']==min(int(line[0]),int(line[1])))&(df['toBus']==max(int(line[0]),int(line[1])))&(df['ID']==line[2])]
                if len(mon_branch_df)>0:
                    filtered_list.append(tuple(mon_branch_df.values[0].tolist()))
            if len(filtered_list)>0:
                brndata = filtered_list
                logFile.info("######################################################################################")
                logFile.info(f"The following lines are monitored: {filtered_list}")
            
        
        
    for fromBus, toBus, ID, flow, nominal_flow,PF,_ in brndata:
        

        a = []
        for genArea, genDictinArea in genDataDict.items():
            for (bus, id), gen_obj in genDictinArea.items():
                transmissionElementEff = genEff_dict.get((fromBus, toBus, ID, bus), 0)
                a.append(transmissionElementEff)
        if sum(abs(np.array(a)))>0:
            an = [-a1 for a1 in a]
            if min([a1 for a1 in an if abs(a1)>0])<0:
                col = 0
                for a1 in an:
                    if abs(a1)>0:
                        A_row.append(row)
                        A_col.append(col)
                        data.append(a1)
                    col = col+1
                if row in [103, 209, 211]:
                    print("yes")
                A_row.append(row)
                A_col.append(col+slack_count)
                data.append(-1.0)
                slack_count +=1
                if nominal_flow*PF*0.98-flow<0:
                    print("yes")
                b.append(nominal_flow*PF*0.98-flow)
                c.append(1000)
                lb.append(0)
                ub.append(1000) 
                row = row+1               
            if min([a1 for a1 in a if abs(a1)>0])<0:
                col = 0
                for a1 in a:
                    if abs(a1)>0:
                        A_row.append(row)
                        A_col.append(col)
                        data.append(a1)
                    col = col+1
                if row in [103, 209, 211]:
                    print("yes")                    
                A_row.append(row)
                A_col.append(col+slack_count)
                b.append(nominal_flow*PF*0.98+flow)
                # if nominal_flow*PF*0.98+flow:
                #     print(fromBus, toBus, ID)                
                data.append(-1.0) 
                slack_count +=1
                c.append(1000)  
                lb.append(0)
                ub.append(1000)         
                row = row+1  
                
    from scipy.sparse import coo_matrix
    try:
        A = coo_matrix((data, (A_row, A_col)), shape=(max(A_row)+1, max(A_col)+1))
        res = lp(np.array(c), A_ub=A, b_ub=np.array(b), bounds=list(zip(lb, ub)))
        # negative_indices = [index for index, value in enumerate(b) if value < 0]
        # for index in negative_indices:
        #     negative_indices1 = [value for  value in negative_indices if value != index]
        #     dense_matrix =  A.toarray()
        #     filtered_dense_matrix = np.delete(dense_matrix, negative_indices1, axis=0)
        #     b1 = np.delete(b, negative_indices1, axis=0)
        #     A1 = coo_matrix(filtered_dense_matrix)     
        #     res = lp(np.array(c), A_ub=A1, b_ub=np.array(b1), bounds=list(zip(lb, ub))) 
        #     if res.status>0:
        #         print("OOPPPS", index)  

    except:
        logFile.error("Issue with formulating the optimization problem. Maybe there are no generation to curtail from?")
        return 0, 0
    if res.status==0:
        logFile.info("The linear optimization has a feasible solution.")
        logFile.info(f"{sum(res.x[0:numGens])}MW curtailment is required.")
        if sum(res.x[numGens::])>0.001:
            logFile.warning(f"Some of the monitored lines in the areas: {areas_toMonitor} might still have thermal violation!")
    else:
        logFile.warning("The linear optimization was NOT feasible.")
        return 0, 0
    #calculate the total generation from slack gen 
    ierr = psspy.bsys(0,0,[ 0.4, 500.],0,[],1,[1520],0,[],0,[])
    ierr, rarray = psspy.amachreal(0, 1, "PGEN")
    SlackGen_ref = sum(rarray[0])
    psspy.bsysdef(0,0)
    
    
    curtailment = 0.0
    curtailment0 = 0.0
    scuccess = 0
    
    for genArea, genDictinArea in genDataDict.items():
        for (bus, id), gen_obj in genDictinArea.items():
            _, genDataDict_temp = getGenData(busData,gen_bus)
            genDictinArea_temp = genDataDict_temp[genArea]
            # print(genDataDict_temp)
            
            if res.x[genDictinArea[(bus, id)].index]>1.0:
                NewGen = genDictinArea_temp[(bus, id)].MW-res.x[genDictinArea[(bus, id)].index]
                delta = 10.0
                oldGen = genDictinArea_temp[(bus, id)].MW
                while abs(NewGen-genDictinArea_temp[(bus, id)].MW)>ALMOSTZERO:
                    gen_decr = 0
                    psspy.bsysdef(0,0)
                    balanceGen = 0
                    if genDictinArea_temp[(bus, id)].MW-delta<NewGen:
                        balanceGen = genDictinArea_temp[(bus, id)].MW-NewGen
                        gen_decr = genDictinArea_temp[(bus, id)].MW - NewGen
                        genDictinArea_temp[(bus, id)].MW = NewGen
                    else:
                        genDictinArea_temp[(bus, id)].MW = genDictinArea_temp[(bus, id)].MW-delta
                        balanceGen = delta
                        gen_decr = delta
                    if genDictinArea_temp[(bus, id)].MW<ALMOSTZERO:
                        gen_status = 0
                    else:
                        gen_status = 1
                    ierr = psspy.machine_chng_3(bus,f"""{id}""",[gen_status,_i,_i,_i,_i,_i,_i],[genDictinArea_temp[(bus, id)].MW ,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f])
                    if ierr>0:
                        print('Error in machine_chng_3')
                        logFile.warning(f"Issue with machine_chng_3 for (bus, id) {(bus, id)}")
                        break                    
                    psspy.solution_parameters_5([_i,100,_i,_i,10,_i,_i,20,0],[_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f])
                    psspy.fnsl([0,0,1,1,2,0,0,0])
                 
                    if ierr>=1:
                        logFile.warning(f"Issue with completing the curtailment for {PreProjectGen_df.loc[PreProjectGen_df['Bus']==bus,'MPID'].values[0]} at bus {bus} and machine id {id}.")
                        logFile.warning(f"Curtailment is done partially, and the proccess is terminated. After {curtailment0} curtailment.")
                        if os.path.isfile(os.path.join(workingcasePath,saveCase)):
                            case_standAlone.reloadCase(os.path.join(workingcasePath,saveCase))
                        else:
                            case_standAlone.reloadCase(os.path.join(workingcasePath,newWorkingCase))
                        break
                    else:
                        curtailment += gen_decr
                        curtailment0 = curtailment
                        ierr = psspy.save(os.path.join(workingcasePath,saveCase))                        

                    if len(inputDF)>0:
                       df = inputDF.dropna(subset=['Area'])
                    else:
                        df = pd.DataFrame()
                    if len(df)>0:    
                        substring_set = ["wind", "solar"]
                        mask = df['Technology'].str.contains('|'.join(substring_set), case=False)  # identify the resources that are wind and solar so that we can exclude them from dispatching them up
                        df = df[~mask]
                        df = df.loc[(df['Area'].astype(int).isin(genDictinArea[(bus, id)].opposingAreas)) & (df['sheet_name']=="GenerationMapping")]
                        ierr = psspy.bsys(0,0,[ 0.4, 500.],0,[],len(df["Bus"].astype(int).tolist()),df["Bus"].astype(int).tolist(),0,[],0,[])
                        if ierr>0:
                            logFile.info(f"Issue in compensating the curtailed value from opposing.") 
                            return curtailment0, scuccess
                    else:
                        psspy.bsysdef(0,0)
                        ierr = psspy.bsys(0,0,[ 0.4, 500.],len(genDictinArea[(bus, id)].opposingAreas),genDictinArea[(bus, id)].opposingAreas,0,[],0,[],0,[])
                    if ierr>0:
                        logFile.info(f"Issue with finding opposing subsystem for substituting machine id={id} at bus={bus}. Using Slack bus to compensate.")
                    else:
                        logFile.info(f"Areas= {genDictinArea[(bus, id)].opposingAreas} are selected as the opposing areas for generator at bus {bus}.")
                        ierr, totals_pre, moto_pre = psspy.scal_4(0,0,1,[0,0,0,0,0,0],[0.0,0.0,0.0,0.0,0.0,0.0,0.0])
                        if ierr>0:
                            logFile.info(f"Issue in scaling using slack bus")             
                        else:
                            ierr, totals_post, moto_post = psspy.scal_4(0,1,2,[_i,0,_i,3,1,1],[0.0, balanceGen,0.0,0.0,0.0,0.0,0.0])
                            if ierr>0:
                                logFile.info(f"Issue in scaling using slack bus")  
                                print('Issue in the second scal_4')    
                            else:
                                psspy.solution_parameters_5([_i,100,_i,_i,10,_i,_i,20,0],[_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f,_f])                 
                                psspy.fnsl([0,0,0,0,2,0,0,0])
                                ierr= psspy.solved()
                                if ierr>=1:
                                    logFile.warning("Issue with solving the case after scaling.")
                                    psspy.case(os.path.join(workingcasePath,saveCase))
                                else:
                                    curtailment0 = curtailment
                                    ierr = psspy.save(os.path.join(workingcasePath,saveCase))
                                    

    ierr = psspy.save(os.path.join(workingcasePath,saveCase))  
    if ierr==0:        
        logFile.info(f"Saving the curtailed case as {saveCase}.")

        
        reportFile = os.path.join(workingcasePath,saveCase.replace(".sav",""))
        logFile.info(f'''Creating a case comparison report pre and post curtailment. The report is saved in {reportFile}.DAT''')
        psspy.report_output(2,reportFile,[2,0])
        psspy.diff(0,1,1,[0,1,0,0],[0.0,0.0,0.0],os.path.join(workingcasePath,workingCase))
        psspy.diff(0,1,2,[0,4,0,0],[ 2.0,0.0,0.0],os.path.join(workingcasePath,workingCase))
        psspy.diff(0,1,3,[0,0,0,0],[0.0,0.0,0.0],os.path.join(workingcasePath,workingCase))
        psspy.progress_output(1,"",[0,0]) # return the report to the console


        logFile.info("Proceeding to intertie schedule adjustments.")
    else:
        logFile.warning("Error is saving the file.")
    # bring the slack gen back to its schedule
    psspy.fnsl([0,0,0,0,2,0,0,0])
    ierr= psspy.solved()
    if ierr>0:
        logFile.warning("Issue with solving the saved case after curtailment. The intertie schedule will deviate from the scheduled one.") 
        return curtailment, 0
    ierr = psspy.bsys(0,0,[ 0.4, 500.],0,[],1,[1520],0,[],0,[])
    ierr, rarray = psspy.amachreal(0, 1, "PGEN")
    SlackGen = sum(rarray[0])
    psspy.bsysdef(0,0)
    max_iter = 100
    itr = 0
    while abs(SlackGen-SlackGen_ref)>15 and itr<max_iter:
        itr = itr+1
        delta = SlackGen-SlackGen_ref
        if delta>0:
            step = 10
        else:
            step = -10
        initial = step
        scaled = step
        ierr = 1


        if len(df)>0:
            df = inputDF.dropna(subset=['Area'])
            substring_set = ["wind", "solar"]
            mask = df['Technology'].str.contains('|'.join(substring_set), case=False)  # identify the resources that are wind and solar so that we can exclude them from dispatching them up
            df = df[~mask]
            df = df.loc[(df['Area'].astype(int).isin(OpposingRegions[studyRegion])) & (df['sheet_name']=="GenerationMapping")]
            ierr = psspy.bsys(0,0,[ 0.4, 500.],0,[],len(df["Bus"].astype(int).tolist()),df["Bus"].astype(int).tolist(),0,[],0,[])
            if ierr>0:
                logFile.info("issue with creating the subsystem from the customized input to scale the generation and bring the intertie back to its schedule")
                logFile.info("Using generic opposing region for scaling the generation to bring the intertie back to its schedule")
            if ierr>0:
                logFile.info(f"Scaling the units in {OpposingRegions[studyRegion]} to bring the intertie back to its scheudle")
                ierr = psspy.bsys(0,0,[ 0.4, 500.],len(OpposingRegions[studyRegion]),OpposingRegions[studyRegion],0,[],0,[],0,[])
        else:
            ierr = psspy.bsys(0,0,[ 0.4, 500.],0,[],1,[1520],0,[],0,[])
        ierr, totals_pre, moto_pre = psspy.scal_4(0,0,1,[0,0,0,0,0,0],[0.0,0.0,0.0,0.0,0.0,0.0,0.0])
        if ierr>0:
            print('Error in the first scal_4')    
            return 0, 0            
        ierr, totals_post, moto_post = psspy.scal_4(0,1,2,[_i,0,_i,3,1,1],[0.0, scaled,0.0,0.0,0.0,0.0,0.0])  
        if ierr>0:
            print('Error in the second scal_4')   
            return 0, 0                  
        psspy.fnsl([0,0,0,0,2,0,0,0])
        ierr= psspy.solved()   
        if ierr>1:
            return 0, 0
        else:
            psspy.save(os.path.join(workingcasePath,saveCase))     


        if ierr>0: logFile.warning("Issue with solving the saved case after curtailment") 
        ierr = psspy.bsys(0,0,[ 0.4, 500.],0,[],1,[1520],0,[],0,[])
        ierr, rarray = psspy.amachreal(0, 1, "PGEN")
        SlackGen = sum(rarray[0])
        psspy.bsysdef(0,0)            

        scaled += step
    scuccess = 1
    reportFile = os.path.join(workingcasePath,saveCase.replace(".sav",""))
    logFile.info(f'''Creating a case comparison report pre and post curtailment. The report is saved in {reportFile}.DAT''')
    psspy.report_output(2,reportFile,[2,0])
    psspy.diff(0,1,1,[0,1,0,0],[0.0,0.0,0.0],os.path.join(workingcasePath,workingCase))
    psspy.diff(0,1,2,[0,4,0,0],[ 2.0,0.0,0.0],os.path.join(workingcasePath,workingCase))
    psspy.diff(0,1,3,[0,0,0,0],[0.0,0.0,0.0],os.path.join(workingcasePath,workingCase))
    psspy.progress_output(1,"",[0,0]) # return the report to the console
    ierr = psspy.close_powerflow()


    return curtailment, scuccess


if __name__ == "__main__":
    try:
        calShiftFactor = 1
        run_local = 1
        if run_local ==1:
            # bus numbers for the generation units to be excluded from the curtailment
            bnb = []
            # area numbers for the generations should not be redispatched to compensate the load-gen balance
            areas_toCuratil = [17]
            areas_toMonitor = [17]
            # passe Case
            orginalcase = ["S5_NW_2029_SP_HG_Post.sav"]
            workingCase = orginalcase
            # direction of the pss\e case
            casePath = r"T:\Rshared\Delivery Shared\Python Files\Sina\GitRepo\cas-automation-tools\CPAT GUI\Functions\Code_test\powerflow"
            # the name of the final N-0 congestion free case
            saveCase = "S5_NW_2029_SP_HG_Post_Curtailed.sav" 
            logFile = log.MyLogger(r"T:\Rshared\Delivery Shared\Python Files\Sina\GitRepo\cas-automation-tools\CPAT GUI\Functions\Code_test\powerflow", "nnn.log")
            df = pd.DataFrame()
            studyRegion = "NorthWest"
        else:
            temp_file_path = sys.argv[1]
            with open(temp_file_path, 'r') as temp_file:
                lines = temp_file.read().splitlines()            
            # area numbers for the generations should not be redispatched to compensate the load-gen balance
            # direction of the pss\e case
            casePath = lines[2]            
            req_issue_dt = lines[0]
            logFile = log.MyLogger(lines[2], f"{req_issue_dt}.log")
            studyRegion = lines[1]
            logFile.info(f"The selected study regions is {studyRegion}.")
            # passe Case
            orginalcase = lines[3]
            
            orginalcase = [s + '.sav' for s in orginalcase.replace('[',"").replace(']',"").split(".sav")[:-1]]
            orginalcase = [s.replace("'","") for s in orginalcase]
            orginalcase = [s.replace(",","") for s in orginalcase]
            orginalcase = [s.lstrip() for s in orginalcase]
            orginalcase = [s.rstrip() for s in orginalcase]       
            workingCase = [s.replace("'","") for s in orginalcase]  
            # workingCase = workingCase[0]    
            # orginalcase = orginalcase[0]  
            # the name of the final N-0 congestion free case
            saveCase = lines[4]
            saveCase = [s + '.sav' for s in saveCase.replace('[',"").replace(']',"").split(".sav")[:-1]]
            saveCase = [s.replace("'","") for s in saveCase]
            saveCase = [s.replace(",","") for s in saveCase]
            saveCase = [s.lstrip() for s in saveCase]
            saveCase = [s.rstrip() for s in saveCase]            
            # saveCase  = saveCase[0]
            logFile.info(f"The final case will be saved as {saveCase}.")
            areas_toMonitor= eval(lines[5])
            areas_toCuratil= eval(lines[6])
            logFile.info(f"The transmission line in the following areas will be monitored {areas_toMonitor}.")
            input_dir = lines[8]
            calShiftFactor  =  not(eval(lines[7]))
            curtailThermal = eval(lines[8])
        
            cusom_Input_file_path = r"T:\Rshared\Delivery Shared\Python Files\GitRepo\cas-automation-tools\CPAT GUI\Functions\CustomInputTemplate.xlsx" 
            MonitoredLines_df = pd.DataFrame()
            try:
                if os.path.exists(os.path.join(lines[9],lines[10])):
                    cusom_Input_file_path = os.path.join(lines[9],lines[10])
                    
                    Input_df = pd.read_csv(cusom_Input_file_path)
                    Gen_mapping_df = Input_df.loc[Input_df['sheet_name']=="GenerationMapping"]
                    Gen_mapping_df = Gen_mapping_df.loc[Gen_mapping_df['Curtail?']=='Yes']
                    Gen_mapping_df.reset_index()
                    Gen_mapping_df.dropna(axis = 0, how = 'all', inplace = True)
                    Gen_mapping_df.dropna(axis = 1, how = 'all', inplace = True)
                    MonitoredLines_df = Input_df.loc[Input_df['sheet_name']=='MonitoredLines']
                    MonitoredLines_df.dropna(axis = 0, how = 'all', inplace = True)
                    MonitoredLines_df.dropna(axis = 1, how = 'all', inplace = True)
                    
                else:
                    Gen_mapping_df = pd.read_excel(cusom_Input_file_path,"GenerationMapping")
                    
            except:
                Gen_mapping_df = pd.read_excel(cusom_Input_file_path,"GenerationMapping")
            if not input_dir=="None":
                try:
                    df = pd.read_csv(os.path.join(input_dir,lines[9])) 
                    logFile.info(f"Customized input file is provided,")
                except:
                    df = pd.DataFrame()
            else:
                df = pd.DataFrame()
        logFile.info(f"The selected working path is {casePath}.")
        for i in range(len(workingCase)):
            logFile.info(f"Working on the curtailment minimization for {workingCase[i]}.")
            if calShiftFactor or (not os.path.exists(os.path.join(casePath,'ShiftFactor1.csv'))):
                logFile.info(f"Calculating ShiftFactors.")
                SF = ShiftFactor()
                SF.process('1', os.path.join(casePath,workingCase[i]),"ShiftFactor1")
                logFile.info(f"Shift Factors are claculated and saved in ShiftFactor1.csv")
            else:
                logFile.info(f'using previously calculated ShiftFactor')
            genEff_df = pd.read_csv(os.path.join(casePath, "ShiftFactor1.csv"))    
            totalCurtailment = 0
            curtailment = 1000
            iterationLimit = 10
            j = 0
            scuccess = 1
            newWorkingCase = workingCase[i]
            while j<iterationLimit and curtailment>10 and scuccess==1:
                PreProjectGen_df = Gen_mapping_df.copy(deep=True)
                curtailment, scuccess = curtail(casePath, newWorkingCase, workingCase[i], saveCase[i], areas_toMonitor, areas_toCuratil, df, studyRegion, curtailThermal, PreProjectGen_df,MonitoredLines_df,  logFile)
                totalCurtailment += curtailment
                newWorkingCase = saveCase[i]

                psspy.psseinit(16000) 
                _i=psspy.getdefaultint()
                _f=psspy.getdefaultreal()
                _s=psspy.getdefaultchar()
                progreeReport = os.path.join(casePath,saveCase[i].replace(".sav","Progress.txt"))
                psspy.report_output(2,progreeReport,[2,0])
                logFile.info(f"The PSS\E progress will be available in {progreeReport}.")
                psspy.case(os.path.join(casePath,saveCase[i]))          
                j +=1
                if curtailment<50: # if curtailment in the iteration is tool small exit
                    break
                else:
                    logFile.info("Trying curtailment one more time, in case additional curtailment is required.")
                try:
                    psspy.close_powerflow()
                except:
                    pass
            
            logFile.info(f"""
                            *********************************************************
                            *********************************************************
                            *********************************************************
                            Total Curtailment for {orginalcase[i]} is {totalCurtailment} MW
                            *********************************************************
                            *********************************************************
                            *********************************************************
                            *********************************************************
                            """)
            psspy.psseinit(16000) 
            _i=psspy.getdefaultint()
            _f=psspy.getdefaultreal()
            _s=psspy.getdefaultchar() 
            psspy.report_output(2,progreeReport,[2,0])
            psspy.case(os.path.join(casePath,saveCase[i] ))

            psspy.bsys(0,1,[ .1, 500.],len(areas_toCuratil),areas_toCuratil,0,[],0,[],0,[])   
            ierr, carray = psspy.amachchar(0, 1, 'ID')
            ierr, iarray =  psspy.amachint(0,1,'NUMBER')
            ierr, rarray = psspy.amachreal(0,1,'PGEN')
            data = {'ID': carray[0][:],'Bus':iarray[0][:],'PGEN': rarray[0][:]}
            PostCurtailment_df = pd.DataFrame(data)    


            psspy.case(os.path.join(casePath,orginalcase[i] ))
            psspy.bsys(0,1,[ .1, 500.],len(areas_toCuratil),areas_toCuratil,0,[],0,[],0,[])   
            ierr, carray = psspy.amachchar(0, 1, 'ID')
            ierr, iarray =  psspy.amachint(0,1,'NUMBER')
            ierr, rarray = psspy.amachreal(0,1,'PGEN')
            data = {'ID': carray[0][:],'Bus':iarray[0][:],'PGEN': rarray[0][:]}
            PreCurtailment_df = pd.DataFrame(data)    
            merged_df = pd.merge(PreCurtailment_df, PostCurtailment_df, on=['ID','Bus'], how='left', suffixes=('_x', '_y'))
            merged_df = merged_df.rename(columns={'PGEN_y': 'PostCurtailment(MW)', 'PGEN_x': 'PreCurtailment(MW)'})  
            merged_df = merged_df.fillna(0)
            merged_df = merged_df.loc[(merged_df['PostCurtailment(MW)']-merged_df['PreCurtailment(MW)'])<-0.0001]
            totalCurtailment = (merged_df['PostCurtailment(MW)']-merged_df['PreCurtailment(MW)']).sum()
            merged_df = merged_df.loc[(merged_df['PostCurtailment(MW)']-merged_df['PreCurtailment(MW)'])<-2]
            PreProjectGen_df = Gen_mapping_df.copy(deep=True)
            PreProjectGen_df = PreProjectGen_df.loc[PreProjectGen_df['Area'].isin(areas_toCuratil)]            
            merged_df['ID'] =  merged_df['ID'].str.replace(' ', '')
            merged_df = pd.merge(merged_df, PreProjectGen_df[['Bus','MPID','ID']], on=['Bus','ID'], how='left' )
            merged_df = merged_df[['MPID','Bus', 'ID', 'PostCurtailment(MW)',  'PreCurtailment(MW)' ]]
            
            merged_df['Difference'] = merged_df['PostCurtailment(MW)'] - merged_df['PreCurtailment(MW)']
            new_row = {'MPID':"Z_Total",'Bus':"N/A", 'ID':"N/A", 'PostCurtailment(MW)':"N/A",  'PreCurtailment(MW)':"N/A", 'Difference': totalCurtailment}
            merged_df =  pd.concat([merged_df, pd.DataFrame([new_row])], ignore_index=True)
            merged_df.to_csv(os.path.join(casePath,orginalcase[i].replace('.sav','.csv') ))
            psspy.diff(0,1,1,[0,0,0,0],[0.0,0.0,0.0],os.path.join(casePath,orginalcase[i] ))
            psspy.diff(0,1,2,[0,4,0,0],[15.0,0.0,0.0],os.path.join(casePath,orginalcase[i]))
            psspy.diff(0,1,3,[0,0,0,0],[0.0,0.0,0.0],os.path.join(casePath,orginalcase[i]))
        logFile.info('Done!')
        print("Success")
    except:
        print("Failed")


