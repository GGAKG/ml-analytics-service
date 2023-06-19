# -----------------------------------------------------------------
# Name : py_survey_streaming.py
# Author :
# Description : Program to read data from one kafka topic and 
#   produce it to another kafka topic
# -----------------------------------------------------------------

from pymongo import MongoClient
from bson.objectid import ObjectId
import sys, os, json, time, re
import datetime
import requests
from configparser import ConfigParser,ExtendedInterpolation
from cassandra.cluster import Cluster
from cassandra.query import SimpleStatement,ConsistencyLevel
from slackclient import SlackClient
import psycopg2
from geopy.distance import geodesic
import logging
import logging.handlers
import time
from logging.handlers import TimedRotatingFileHandler
import redis

config_path = os.path.split(os.path.dirname(os.path.abspath(__file__)))
config = ConfigParser(interpolation=ExtendedInterpolation())
config.read(config_path[0] + "/config.ini")
sys.path.append(config.get("COMMON", "cloud_module_path"))

from cloud import MultiCloud
cloud_init = MultiCloud()

# date formating
current_date = datetime.date.today()
formatted_current_date = current_date.strftime("%d-%B-%Y")
number_of_days_logs_kept = current_date - datetime.timedelta(days=7)
number_of_days_logs_kept = number_of_days_logs_kept.strftime("%d-%B-%Y")

# file path for log
file_path_for_output_and_debug_log = config.get('LOGS', 'survey_streaming_success_error')
file_name_for_output_log = f"{file_path_for_output_and_debug_log}{formatted_current_date}-output.log"
file_name_for_debug_log = f"{file_path_for_output_and_debug_log}{formatted_current_date}-debug.log"

# Remove old log entries 
files_with_date_pattern = [file 
for file in os.listdir(file_path_for_output_and_debug_log) 
if re.match(r"\d{2}-\w+-\d{4}-*", 
file)]

for file_name in files_with_date_pattern:
    file_path = os.path.join(file_path_for_output_and_debug_log, file_name)
    if os.path.isfile(file_path):
        file_date = file_name.split('.')[0]
        date = file_date.split('-')[0] + '-' + file_date.split('-')[1] + '-' + file_date.split('-')[2]
        if date < number_of_days_logs_kept:
            os.remove(file_path)

# Add loggers
formatter = logging.Formatter('%(asctime)s - %(levelname)s')

# handler for output and debug Log
output_logHandler = RotatingFileHandler(f"{file_name_for_output_log}")
output_logHandler.setFormatter(formatter)

debug_logHandler = RotatingFileHandler(f"{file_name_for_debug_log}")
debug_logHandler.setFormatter(formatter)

# Add the successLoger
successLogger = logging.getLogger('success log')
successLogger.setLevel(logging.DEBUG)
successBackuphandler = TimedRotatingFileHandler(f"{file_name_for_output_log}", when="w0",backupCount=1)
successLogger.addHandler(output_logHandler)
successLogger.addHandler(successBackuphandler)

# Add the Errorloger
errorLogger = logging.getLogger('error log')
errorLogger.setLevel(logging.ERROR)
errorBackuphandler = TimedRotatingFileHandler(f"{file_name_for_output_log}",when="w0",backupCount=1)
errorLogger.addHandler(output_logHandler)
errorLogger.addHandler(errorBackuphandler)

# Add the Infologer
infoLogger = logging.getLogger('info log')
infoLogger.setLevel(logging.INFO)
debug_logBackuphandler = TimedRotatingFileHandler(f"{file_name_for_debug_log}",when="w0",backupCount=1)
infoLogger.addHandler(debug_logHandler)
infoLogger.addHandler(debug_logBackuphandler)

try:    

    #db production
    client = MongoClient(config.get('MONGO', 'url'))
    db = client[config.get('MONGO', 'database_name')]
    surveySubmissionsCollec = db[config.get('MONGO', 'survey_submissions_collection')]
    solutionsCollec = db[config.get('MONGO', 'solutions_collection')]
    surveyCollec = db[config.get('MONGO', 'survey_collection')]
    questionsCollec = db[config.get('MONGO', 'questions_collection')]
    criteriaCollec = db[config.get('MONGO', 'criteria_collection')]
    programsCollec = db[config.get('MONGO', 'programs_collection')]

    # redis cache connection 
    redis_connection = redis.ConnectionPool(
        host=config.get("REDIS", "host"), 
        decode_responses=True, 
        port=config.get("REDIS", "port"), 
        db=config.get("REDIS", "db_name")
    )
    datastore = redis.StrictRedis(connection_pool=redis_connection)

except Exception as e:
    errorLogger.error(e, exc_info=True)


try:
    def obj_creation(msg_id):
        successLogger.debug("Survey Submission Id : " + str(msg_id))
        cursorMongo = surveySubmissionsCollec.find(
            {'_id':ObjectId(msg_id)}, no_cursor_timeout=True
        )
        for obSub in cursorMongo :
         if 'isAPrivateProgram' in obSub :
            surveySubQuestionsArr = []
            completedDate = str(obSub['completedDate'])
            createdAt = str(obSub['createdAt'])
            updatedAt = str(obSub['updatedAt'])
            evidencesArr = [v for v in obSub['evidences'].values()]
            evidence_sub_count = 0

            # fetch user name from postgres with the help of keycloak id
            userObj = {}
            userObj = datastore.hgetall("user:" + obSub["createdBy"])
            rootOrgId = None
            orgName = None
            if userObj :
                try:
                    rootOrgId = userObj["rootorgid"]
                except KeyError :
                    rootOrgId = ''
                try:
                    orgName = userObj["orgname"]
                except KeyError:
                    orgName = ''
            if 'answers' in obSub.keys() :  
                    answersArr = [v for v in obSub['answers'].values()]
                    for ans in answersArr:
                        try:
                            if len(ans['fileName']):
                                evidence_sub_count = evidence_sub_count + len(ans['fileName'])
                        except KeyError:
                            pass
                    for ans in answersArr:
                        def sequenceNumber(externalId,answer):
                            for solu in solutionsCollec.find({'_id':ObjectId(obSub['solutionId'])}):
                                section =  [k for k in solu['sections'].keys()]
                                # parsing through questionSequencebyecm to get the sequence number
                                try:
                                    for num in range(
                                        len(solu['questionSequenceByEcm'][answer['evidenceMethod']][section[0]])
                                    ):
                                        if solu['questionSequenceByEcm'][answer['evidenceMethod']][section[0]][num] == externalId:
                                            return num + 1
                                except KeyError:
                                    pass
                        
                        def creatingObj(answer,quesexternalId,ans_val,instNumber,responseLabel):
                            surveySubQuestionsObj = {}
                            try:
                                surveySubQuestionsObj['appName'] = obSub["appInformation"]["appName"].lower()
                            except KeyError :
                                surveySubQuestionsObj['appName'] = config.get("ML_APP_NAME", "survey_app")

                            surveySubQuestionsObj['surveySubmissionId'] = str(obSub['_id'])

                            surveySubQuestionsObj['createdBy'] = obSub['createdBy']

                            try:
                                surveySubQuestionsObj['isAPrivateProgram'] = obSub['isAPrivateProgram']
                            except KeyError:
                                surveySubQuestionsObj['isAPrivateProgram'] = True

                            try:
                                surveySubQuestionsObj['programExternalId'] = obSub['programExternalId']
                            except KeyError :
                                surveySubQuestionsObj['programExternalId'] = None
                            try:
                                surveySubQuestionsObj['programId'] = str(obSub['programId'])
                            except KeyError :
                                surveySubQuestionsObj['programId'] = None
                            try:
                                for program in programsCollec.find({'externalId':obSub['programExternalId']}):
                                    surveySubQuestionsObj['programName'] = program['name']
                            except KeyError :
                                surveySubQuestionsObj['programName'] = None

                            surveySubQuestionsObj['solutionExternalId'] = obSub['solutionExternalId']
                            surveySubQuestionsObj['surveyId'] = str(obSub['surveyId'])
                            for solu in solutionsCollec.find({'_id':ObjectId(obSub['solutionId'])}):
                                surveySubQuestionsObj['solutionId'] = str(solu["_id"])
                                surveySubQuestionsObj['solutionName'] = solu['name']
                                section = [k for k in solu['sections'].keys()]
                                surveySubQuestionsObj['section'] = section[0]
                                surveySubQuestionsObj['questionSequenceByEcm']= sequenceNumber(quesexternalId, answer)
                                try:
                                    if solu['scoringSystem'] == 'pointsBasedScoring':
                                        try:
                                            surveySubQuestionsObj['totalScore'] = obSub['pointsBasedMaxScore']
                                        except KeyError :
                                            surveySubQuestionsObj['totalScore'] = ''
                                        try:
                                            surveySubQuestionsObj['scoreAchieved'] = obSub['pointsBasedScoreAchieved']
                                        except KeyError :
                                            surveySubQuestionsObj['scoreAchieved'] = ''
                                        try:
                                            surveySubQuestionsObj['totalpercentage'] = obSub['pointsBasedPercentageScore']
                                        except KeyError :
                                            surveySubQuestionsObj['totalpercentage'] = ''
                                        try:
                                            surveySubQuestionsObj['maxScore'] = answer['maxScore']
                                        except KeyError :
                                            surveySubQuestionsObj['maxScore'] = ''
                                        try:
                                            surveySubQuestionsObj['minScore'] = answer['scoreAchieved']
                                        except KeyError :
                                            surveySubQuestionsObj['minScore'] = ''
                                        try:
                                            surveySubQuestionsObj['percentageScore'] = answer['percentageScore']
                                        except KeyError :
                                            surveySubQuestionsObj['percentageScore'] = ''
                                        try:
                                            surveySubQuestionsObj['pointsBasedScoreInParent'] = answer['pointsBasedScoreInParent']
                                        except KeyError :
                                            surveySubQuestionsObj['pointsBasedScoreInParent'] = ''
                                except KeyError:
                                    surveySubQuestionsObj['totalScore'] = ''
                                    surveySubQuestionsObj['scoreAchieved'] = ''
                                    surveySubQuestionsObj['totalpercentage'] = ''
                                    surveySubQuestionsObj['maxScore'] = ''
                                    surveySubQuestionsObj['minScore'] = ''
                                    surveySubQuestionsObj['percentageScore'] = ''
                                    surveySubQuestionsObj['pointsBasedScoreInParent'] = ''

                            if 'surveyInformation' in obSub :
                             if 'name' in obSub['surveyInformation']:
                              surveySubQuestionsObj['surveyName'] = obSub['surveyInformation']['name']
                             else :
                              try:
                               for ob in surveyCollec.find({'_id':obSub['surveyId']},{'name':1}):
                                surveySubQuestionsObj['surveyName'] = ob['name']
                              except KeyError :
                               surveySubQuestionsObj['surveyName'] = ''
                            else :
                             try:
                              for ob in surveyCollec.find({'_id':obSub['surveyId']},{'name':1}):
                               surveySubQuestionsObj['surveyName'] = ob['name']
                             except KeyError :
                              surveySubQuestionsObj['surveyName'] = ''
                            surveySubQuestionsObj['questionId'] = str(answer['qid'])
                            surveySubQuestionsObj['questionAnswer'] = ans_val
                            surveySubQuestionsObj['questionResponseType'] = answer['responseType']
                            if answer['responseType'] == 'number':
                                if answer['payload']['labels']:
                                    surveySubQuestionsObj['questionResponseLabel_number'] = responseLabel
                                else:
                                    surveySubQuestionsObj['questionResponseLabel_number'] = ''
                            try:
                             if answer['payload']['labels']:
                                if answer['responseType'] == 'text':
                                 surveySubQuestionsObj['questionResponseLabel'] = "'"+ re.sub("\n|\"","",responseLabel) +"'"
                                else:
                                 surveySubQuestionsObj['questionResponseLabel'] = responseLabel
                             else:
                                surveySubQuestionsObj['questionResponseLabel'] = ''
                            except KeyError :
                                surveySubQuestionsObj['questionResponseLabel'] = ''
                            surveySubQuestionsObj['questionExternalId'] = quesexternalId
                            surveySubQuestionsObj['questionName'] = answer['payload']['question'][0]
                            surveySubQuestionsObj['questionECM'] = answer['evidenceMethod']
                            surveySubQuestionsObj['criteriaId'] = str(answer['criteriaId'])
                            for crit in criteriaCollec.find({'_id':ObjectId(answer['criteriaId'])}):
                                surveySubQuestionsObj['criteriaExternalId'] = crit['externalId']
                                surveySubQuestionsObj['criteriaName'] = crit['name']
                            surveySubQuestionsObj['completedDate'] = completedDate
                            surveySubQuestionsObj['createdAt'] = createdAt
                            surveySubQuestionsObj['updatedAt'] = updatedAt
                            if answer['remarks'] :
                             surveySubQuestionsObj['remarks'] = "'"+ re.sub("\n|\"","",answer['remarks']) +"'"
                            else :
                             surveySubQuestionsObj['remarks'] = None
                            if len(answer['fileName']):
                                multipleFiles = None
                                fileCnt = 1
                                for filedetail in answer['fileName']:
                                    if fileCnt == 1:
                                        multipleFiles = config.get('ML_SURVEY_SERVICE_URL', 'evidence_base_url') + filedetail['sourcePath']
                                        fileCnt = fileCnt + 1
                                    else:
                                        multipleFiles = multipleFiles + ' , ' + config.get('ML_SURVEY_SERVICE_URL', 'evidence_base_url') + filedetail['sourcePath']
                                surveySubQuestionsObj['evidences'] = multipleFiles                                  
                                surveySubQuestionsObj['evidence_count'] = len(answer['fileName'])
                            surveySubQuestionsObj['total_evidences'] = evidence_sub_count
                            # to fetch the parent question of matrix
                            if ans['responseType']=='matrix':
                                surveySubQuestionsObj['instanceParentQuestion'] = ans['payload']['question'][0]
                                surveySubQuestionsObj['instanceParentId'] = ans['qid']
                                surveySubQuestionsObj['instanceParentResponsetype'] =ans['responseType']
                                surveySubQuestionsObj['instanceParentCriteriaId'] =ans['criteriaId']
                                for crit in criteriaCollec.find({'_id':ObjectId(ans['criteriaId'])}):
                                    surveySubQuestionsObj['instanceParentCriteriaExternalId'] = crit['externalId']
                                    surveySubQuestionsObj['instanceParentCriteriaName'] = crit['name']
                                surveySubQuestionsObj['instanceId'] = instNumber
                                for ques in questionsCollec.find({'_id':ObjectId(ans['qid'])}):
                                    surveySubQuestionsObj['instanceParentExternalId'] = ques['externalId']
                                surveySubQuestionsObj['instanceParentEcmSequence']= sequenceNumber(
                                    surveySubQuestionsObj['instanceParentExternalId'], answer
                                )
                            else:
                                surveySubQuestionsObj['instanceParentQuestion'] = ''
                                surveySubQuestionsObj['instanceParentId'] = ''
                                surveySubQuestionsObj['instanceParentResponsetype'] =''
                                surveySubQuestionsObj['instanceId'] = instNumber
                                surveySubQuestionsObj['instanceParentExternalId'] = ''
                                surveySubQuestionsObj['instanceParentEcmSequence'] = '' 
                            surveySubQuestionsObj['channel'] = rootOrgId 
                            surveySubQuestionsObj['parent_channel'] = "SHIKSHALOKAM"
                            surveySubQuestionsObj['organisation_name'] = orgName
                            return surveySubQuestionsObj

                        # fetching the question details from questions collection
                        def fetchingQuestiondetails(ansFn,instNumber):        
                            for ques in questionsCollec.find({'_id':ObjectId(ansFn['qid'])}):
                                if len(ques['options']) == 0:
                                    try:
                                        if len(ansFn['payload']['labels']) > 0:
                                            finalObj = {}
                                            finalObj =  creatingObj(
                                                ansFn,ques['externalId'],
                                                ansFn['value'],
                                                instNumber,
                                                ansFn['payload']['labels'][0]
                                            )
                                            json.dump(finalObj, f)
                                            f.write("\n")
                                            successLogger.debug("Send Obj to Azure")
                                    except KeyError :
                                        pass 
                                else:
                                    labelIndex = 0
                                    for quesOpt in ques['options']:
                                        try:
                                            if type(ansFn['value']) == str or type(ansFn['value']) == int:
                                                if quesOpt['value'] == ansFn['value'] :
                                                    finalObj = {}
                                                    finalObj =  creatingObj(
                                                        ansFn,ques['externalId'],
                                                        ansFn['value'],
                                                        instNumber,
                                                        ansFn['payload']['labels'][0]
                                                    )
                                                    json.dump(finalObj, f)
                                                    f.write("\n")
                                                    successLogger.debug("Send Obj to Azure") 
                                            elif type(ansFn['value']) == list:
                                                for ansArr in ansFn['value']:
                                                    if quesOpt['value'] == ansArr:
                                                        finalObj = {}
                                                        finalObj =  creatingObj(
                                                            ansFn,ques['externalId'],
                                                            ansArr,
                                                            instNumber,
                                                            quesOpt['label']
                                                        )
                                                        json.dump(finalObj, f)
                                                        f.write("\n")
                                                        successLogger.debug("Send Obj to Azure")
                                        except KeyError:
                                            pass
                                        
                                # #to check the value is null ie is not answered
                                # try:
                                #     if type(ansFn['value']) == str and ansFn['value'] == '':
                                #         finalObj = {}
                                #         finalObj =  creatingObj(
                                #             ansFn,ques['externalId'], ansFn['value'], instNumber, None
                                #         )
                                #         json.dump(finalObj, f)
                                #         f.write("\n")
                                #         successLogger.debug("Send Obj to Azure")
                                # except KeyError:
                                #     pass

                        if (
                            ans['responseType'] == 'text' or ans['responseType'] == 'radio' or 
                            ans['responseType'] == 'multiselect' or ans['responseType'] == 'slider' or 
                            ans['responseType'] == 'number' or ans['responseType'] == 'date'
                        ):   
                            inst_cnt = ''
                            fetchingQuestiondetails(ans, inst_cnt)
                        elif ans['responseType'] == 'matrix' and len(ans['value']) > 0:
                            inst_cnt =0
                            for instances in ans['value']:
                                inst_cnt = inst_cnt + 1
                                for instance in instances.values():
                                    fetchingQuestiondetails(instance,inst_cnt)

        cursorMongo.close()
except Exception as e:
    errorLogger.error(e, exc_info=True)

with open('sl_survey.json', 'w') as f:
 for msg_data in surveySubmissionsCollec.find({"status":"completed"}):
    obj_creation(msg_data['_id'])



local_path = config.get("OUTPUT_DIR", "survey")
blob_path = config.get("COMMON", "survey_blob_path")

for files in os.listdir(local_path):
    if "sl_survey.json" in files:
        cloud_init.upload_to_cloud(blob_Path = blob_path, local_Path = local_path, file_Name = files)
        
payload = {}
payload = json.loads(config.get("DRUID","survey_injestion_spec"))
datasource = [payload["spec"]["dataSchema"]["dataSource"]]
ingestion_spec = [json.dumps(payload)]       
for i, j in zip(datasource,ingestion_spec):
    druid_end_point = config.get("DRUID", "metadata_url") + i
    druid_batch_end_point = config.get("DRUID", "batch_url")
    headers = {'Content-Type' : 'application/json'}
    get_timestamp = requests.get(druid_end_point, headers=headers)
    if get_timestamp.status_code == 200:
        successLogger.debug("Successfully fetched time stamp of the datasource " + i )
        timestamp = get_timestamp.json()
        #calculating interval from druid get api
        minTime = timestamp["segments"]["minTime"]
        maxTime = timestamp["segments"]["maxTime"]
        min1 = datetime.datetime.strptime(minTime, "%Y-%m-%dT%H:%M:%S.%fZ")
        max1 = datetime.datetime.strptime(maxTime, "%Y-%m-%dT%H:%M:%S.%fZ")
        new_format = "%Y-%m-%d"
        min1.strftime(new_format)
        max1.strftime(new_format)
        minmonth = "{:02d}".format(min1.month)
        maxmonth = "{:02d}".format(max1.month)
        min2 = str(min1.year) + "-" + minmonth + "-" + str(min1.day)
        max2 = str(max1.year) + "-" + maxmonth  + "-" + str(max1.day)
        interval = min2 + "_" + max2
        time.sleep(50)

        disable_datasource = requests.delete(druid_end_point, headers=headers)

        if disable_datasource.status_code == 200:
            successLogger.debug("successfully disabled the datasource " + i)
            time.sleep(300)
          
            delete_segments = requests.delete(
                druid_end_point + "/intervals/" + interval, headers=headers
            )
            if delete_segments.status_code == 200:
                successLogger.debug("successfully deleted the segments " + i)
                time.sleep(300)

                enable_datasource = requests.get(druid_end_point, headers=headers)
                if enable_datasource.status_code == 204:
                    successLogger.debug("successfully enabled the datasource " + i)
                    
                    time.sleep(300)

                    start_supervisor = requests.post(
                        druid_batch_end_point, data=j, headers=headers
                    )
                    successLogger.debug("ingest data")
                    if start_supervisor.status_code == 200:
                        successLogger.debug(
                            "started the batch ingestion task sucessfully for the datasource " + i
                        )
                        time.sleep(50)
                    else:
                        errorLogger.error(
                            "failed to start batch ingestion task" + str(start_supervisor.status_code)
                        )
                else:
                    errorLogger.error("failed to enable the datasource " + i)
            else:
                errorLogger.error("failed to delete the segments of the datasource " + i)
        else:
            errorLogger.error("failed to disable the datasource " + i)

    elif get_timestamp.status_code == 204:
        start_supervisor = requests.post(
            druid_batch_end_point, data=j, headers=headers
        )
        if start_supervisor.status_code == 200:
            successLogger.debug(
                "started the batch ingestion task sucessfully for the datasource " + i
            )
            time.sleep(50)
        else:
            errorLogger.error(start_supervisor.text)
            errorLogger.error(
                "failed to start batch ingestion task" + str(start_supervisor.status_code)
            )

