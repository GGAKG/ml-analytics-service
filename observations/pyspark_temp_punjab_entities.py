import shutil
import logging
import requests
import datetime
import argparse
from datetime import date
import json, csv, sys, os, time
from configparser import ConfigParser, ExtendedInterpolation
from pyspark.sql import SparkSession
from pyspark.sql.functions import *
import pyspark.sql.functions as F
from pyspark.sql.types import *
from pyspark.sql import Row
from azure.storage.blob import BlockBlobService, PublicAccess
from azure.storage.blob import ContentSettings
from logging.handlers import TimedRotatingFileHandler, RotatingFileHandler
from pyspark.sql import DataFrame
from pyspark.sql.functions import element_at, split, col

# ------------- Logger START --------------------- #
config_path = os.path.split(os.path.dirname(os.path.abspath(__file__)))
config = ConfigParser(interpolation=ExtendedInterpolation())
config.read(config_path[0] + "/config.ini")
formatter = logging.Formatter('%(asctime)s - %(levelname)s')

#Success
successLogger = logging.getLogger('success log')
successLogger.setLevel(logging.DEBUG)
successHandler = RotatingFileHandler(config.get('LOGS','observation_status_success'))
successBackuphandler = TimedRotatingFileHandler(config.get('LOGS','observation_status_success'),when="w0",backupCount=1)
successHandler.setFormatter(formatter)
successLogger.addHandler(successHandler)
successLogger.addHandler(successBackuphandler)

# Error
errorLogger = logging.getLogger('error log')
errorLogger.setLevel(logging.ERROR)
errorHandler = RotatingFileHandler(config.get('LOGS','observation_status_error'))
errorBackuphandler = TimedRotatingFileHandler(config.get('LOGS','observation_status_error'),when="w0",backupCount=1)
errorHandler.setFormatter(formatter)
errorLogger.addHandler(errorHandler)
errorLogger.addHandler(errorBackuphandler)
# ---------------- Logger END --------------------- #

# Gather data from argParser 
details = argparse.ArgumentParser(description='Pass the ProgramID as key and SolutionID as "_" separated values')
details.add_argument('-e',metavar='--entity', type=str, help='Program IDs & Solution IDs of the required CSV', required=True)
args = details.parse_args()
program_ID = json.loads((args.e).replace('_', ','))

class Df_Creation:
   '''Create dataframe from json data and also helps gather name of arguments passed'''
   def create(self, data):
      '''Creates a dataframe from a given json structure'''
      df = sc.parallelize(data).map(lambda data_frame: json.dumps(data_frame))
      df = spark.read.json(df)
      return df

   def gather_name(self, data, sid):
      '''Gathers the name of the ProgramID and Solution ID that is passed'''
      names = []
      try:
         if sid == data[0]["solutionId"]:
            return data[0]["solutionName"].replace(' ', '_'), data[0]['programName'].replace(' ', '_')
      except KeyError:
         if sid == data[0]["solution_id"]:
            return data[0]["solution_name"].replace(' ', '_'),data[0]['program_name'].replace(' ', '_')


class API:
   '''Gathers the access key and return the new enitity observed data'''
   def __init__(self):
      self.url = "https://staging.sunbirded.org//auth/realms/sunbird/protocol/openid-connect/token"
      self.header = {"Content-Type": "application/x-www-form-urlencoded"}
      self.client = {"client_id": "lms","client_secret": "80ea98b5-f8a3-4745-8994-8bf41d75642e",
                     "grant_type" : "client_credentials","scope": "offline_access"}
      self.token = requests.post(url=self.url, headers=self.header, data=self.client).json()["access_token"]
      self.data_header = {"Authorization" : "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiIxNWJkZDRhMWMwOTc0N2EwOWRkZDdmOTQxMTkzZWYxOSJ9.EDGxHfZB-gyTHjF1yFS9Jek5J0uiFjRz0VwR7YTN-fE",
                           "Content-Type" : "application/json",
                           "X-authenticated-user-token" : self.token
                           }

   def call_data(self, entity_id):
      '''Returns the data from the entities API in JSON format'''
      data_url = f"http://11.3.0.4:8000/private/mlsurvey/api/v1/entities/relatedEntities/{entity_id}"
      data = requests.post(url=data_url, headers=self.data_header)
      return data.json()

   def get_data(self, entity_collection):
      '''Pre-process the data in the entity collection that is passed'''
      gathered_data = []
      for val in entity_collection:
         new_data = self.call_data(val)
         gathered_data.append(new_data)
      return gathered_data


class Entity:
   '''Process and organize the data from the entity API and help create a new df with entity'''
   def __init__(self):
      self.gathered_entities = []
   
   def entity_sepration(self, value):
      '''Takes in a json structure and formats the data in an organized way'''
      for items in value:
         entity_breakdown = {}
         entity_breakdown["merge_key"] = items['result']['_id']
         entity_breakdown[f"entity_observed_{items['result']['entityType']}_id"] = items['result']['_id']
         entity_breakdown[f"Id_of_{items['result']['entityType']}_observed"] = items['result']['metaInformation']['externalId']
         entity_breakdown[f"{items['result']['entityType']}_observed"] = items['result']['metaInformation']['name']
         for val in items['result']['relatedEntities']:
            entity_breakdown[f"entity_observed_{val['entityType']}_id"] = val['_id']
            entity_breakdown[f"Id_of_{val['entityType']}_observed"] = val['metaInformation']['externalId']
            entity_breakdown[f"{val['entityType']}_observed"] = val['metaInformation']['name']
         self.gathered_entities.append(entity_breakdown)
      return self.gathered_entities

   def create_new_df(self, unclear_data):
      '''Creates a new dataframe from the newly gathered data from the entity API'''
      final_entity = self.entity_sepration(unclear_data)
      new_df = sc.parallelize(final_entity)
      new_entity_df = spark.read.json(new_df)
      return new_entity_df


# Create a Spark session
spark = SparkSession.builder.appName("obs_sub_status"
).config("spark.driver.memory", "20g"
).config("spark.executor.memory", "100g"
).config("spark.memory.offHeap.enabled", True
).config("spark.memory.offHeap.size", "32g"
).getOrCreate()
sc = spark.sparkContext

# Start the loop for each solution_ids
for pid, values in program_ID.items():
   for sid in values:
# Druid Query
      url_druid =  "http://11.3.2.25:8082/druid/v2?pretty"
      query = {"obs" : 
                  {"queryType": "scan",
                  "dataSource": "sl-observation",
                  "resultFormat": "list",
                  "columns":["createdBy","user_type","role_title","user_stateName","user_districtName","user_blockName","school_code","solutionId",
                           "user_schoolName","user_boardName","organisation_name","programName","programExternalId","solutionName","solutionExternalId",
                           "entity","observationSubmissionId","questionExternalId","questionName","questionResponseLabel","minScore","evidences","remarks"],
                  "intervals": ["1901-01-01/2101-01-01"],
                  "batchSize":20480,
                  "filter":{"type":"and","fields":[{"type":"selector","dimension":"programId","value":f"{pid}"},
                           {"type":"selector","dimension":"solutionId","value":f"{sid}"}]}},

         "obs_status" : 
                  {"queryType": "scan",
                  "dataSource": "sl-observation-status",
                  "resultFormat": "list",
                  "columns":["user_id","user_type","role_title","state_name","district_name","block_name","school_code","school_name","board_name", 
                           "organisation_name","program_name","program_externalId","solution_name","solution_externalId","entity_id","submission_id",
                           "status","completedDate","solution_id"],
                  "intervals": ["1901-01-01/2101-01-01"],
                  "batchSize":20480,
                  "filter":{"type":"and","fields":[{"type":"selector","dimension":"program_id","value":f"{pid}"},
                           {"type":"selector","dimension":"solution_id","value":f"{sid}"}]}}}


# Query Druid to gather the data
      prev_data = {}            
      for keys in query:
         response = requests.post(url_druid, headers={"Content-Type": "application/json"}, data=json.dumps(query[keys]))
         try:
             data_list = response.json()[0]['events']
             prev_data[keys] = data_list
         except IndexError:
             errorLogger.error(f"Wrong Program ID or Solution ID provided")
             sys.exit()

# Creating the dataframe from druid data and generating the name
      dataframe = Df_Creation()
      obs_df = dataframe.create(prev_data['obs'])
      obs_status_df = dataframe.create(prev_data['obs_status']) 
      obs_name = dataframe.gather_name(prev_data['obs'], sid)

# Pull out the data of entity and distinct values
      entity_df = obs_df.select(obs_df["entity"]).distinct()
      entity_value = entity_df.rdd.flatMap(lambda x: x).collect()

# Pull out the data of entity status and distinct values
      enitity_status_df = obs_status_df.select(obs_status_df["entity_id"]).distinct()
      entity_status_value = enitity_status_df.rdd.flatMap(lambda x: x).collect()

# Gather the observed data from the API call
      get_observed_data = API()
      new_obs_data = get_observed_data.get_data(entity_value)
      new_obs_status_data = get_observed_data.get_data(entity_status_value)

# Create the new observed dataframe
      obs_final_df = Entity()
      new_obs_entity_df = obs_final_df.create_new_df(new_obs_data)
      new_obs_status_entity_df = obs_final_df.create_new_df(new_obs_status_data)
   
# Merge two dataframes into one final dataframe - OBS
      final_obs_df = obs_df.join(new_obs_entity_df, obs_df.entity == new_obs_entity_df.merge_key, "full")
      final_obs_df = final_obs_df.drop("entity", "merge_key") 
      final_obs_df = final_obs_df.withColumnRenamed("createdBy", "UUID")\
			.withColumnRenamed("evidences", "Evidences")\
			.withColumnRenamed("minScore", "Question Score")\
			.withColumnRenamed("observationSubmissionId", "observation_submission_id")\
			.withColumnRenamed("organisation_name", "Org Name")\
			.withColumnRenamed("programExternalId","Program ID")\
			.withColumnRenamed("programName", "Program Name")\
			.withColumnRenamed("questionExternalId", "Question_external_id")\
			.withColumnRenamed("questionName", "Question")\
			.withColumnRenamed("remarks", "Remarks")\
			.withColumnRenamed("role_title", "User Sub Type")\
			.withColumnRenamed("user_blockName", "Declared Block")\
			.withColumnRenamed("user_districtName", "Declared District")\
			.withColumnRenamed("user_boardName", "Declared Board")\
			.withColumnRenamed("user_schoolName", "Declared School")\
			.withColumnRenamed("user_stateName", "Declared State")\
			.withColumnRenamed("user_type", "User Type")\
			.withColumnRenamed("solutionName", "Observation Name")\
			.withColumnRenamed("solutionExternalId", "Observation ID")
   
      final_obs_df = final_obs_df.select("UUID", "User Type", "User Sub Type", "Declared State", "Declared District", "Declared Block", "Declared School", "Declared Board", "Org Name", "Program Name", "Program ID","Observation Name", "Observation ID", "district_observed", "block_observed", "school_observed", "Id_of_school_observed", "observation_submission_id", "Question_external_id","Question", "questionResponseLabel", "Question Score", "Evidences", "Remarks")
      final_obs_df = final_obs_df.sort(col("UUID").desc(), col("Program ID").desc(), col("Observation ID").desc(), col("Observation_submission_id").desc(),col("Question_external_id").desc())
      final_obs_df.printSchema()
   
# Merge two dataframes into one final dataframe - OBS_status   
      final_obs_status_df = obs_status_df.join(new_obs_status_entity_df, obs_status_df.entity_id == new_obs_status_entity_df.merge_key, "full")
      final_obs_status_df = final_obs_status_df.drop("entity_id", "merge_key")
      final_obs_status_df = final_obs_status_df.withColumnRenamed("user_id", "UUID")\
				.withColumnRenamed("block_name", "Declared Block")\
				.withColumnRenamed("board_name", "Declared Board")\
				.withColumnRenamed("completedDate", "Submission date")\
				.withColumnRenamed("district_name", "Declared District")\
				.withColumnRenamed("organisation_name", "Org Name")\
				.withColumnRenamed("program_externalId", "Program ID")\
				.withColumnRenamed("program_name", "Program Name")\
				.withColumnRenamed("role_title", "User Sub Type")\
				.withColumnRenamed("solution_name", "Observation Name")\
	                        .withColumnRenamed("solution_externalId", "Observation ID")\
				.withColumnRenamed("user_type", "User Type")\
				.withColumnRenamed("status", "Status of submission")\
	                        .withColumnRenamed("school_name", "Declared School")\
			        .withColumnRenamed("state_name", "Declared State")\
				.withColumnRenamed("submission_id", "Observation_submission_id")
      final_obs_status_df = final_obs_status_df.select("UUID", "User Type", "User Sub Type", "Declared State", "Declared District", "Declared Block", "Declared School", "Declared Board", "Org Name", "Program Name", "Program ID","Observation Name", "Observation ID", "district_observed", "block_observed", "school_observed", "Id_of_school_observed", "observation_submission_id","Status of submission", "Submission date")
      final_obs_status_df = final_obs_status_df.sort(col("UUID").desc(), col("Program ID").desc(), col("Observation ID").desc(), col("Observation_submission_id").desc())
      final_obs_status_df.printSchema()

# Convert the data into a csv based on program name and solution name
      clock = datetime.datetime.now().date()
      save_path = f"/opt/sparkjobs/source/observations/reports/punjab_observed_data/{obs_name[1]}_{clock}/{obs_name[0]}"
      final_obs_df.coalesce(1).write.option("header", True).mode('overwrite').csv(f"{save_path}/ml_obs/")
      final_obs_status_df.coalesce(1).write.option("header", True).mode('overwrite').csv(f"{save_path}/ml_obs_status")

# Zipping the files on Program Name
   zip_path = "/opt/sparkjobs/source/observations/reports/punjab_observed_data"
   shutil.make_archive(f"{zip_path}/{obs_name[1]}_{clock}", 'zip', f"{zip_path}", f"{obs_name[1]}_{clock}")
   shutil.rmtree(f"{zip_path}/{obs_name[1]}_{clock}")

# Store data into Azure
   blob_service_client = BlockBlobService(
       account_name=config.get("AZURE", "account_name"), 
       account_key=config.get("AZURE", "account_key")
   )
   container_name = config.get("AZURE", "container_name")
   local_path = config.get("OUTPUT_DIR", "punjab_observation_data_local_path")
   blob_path =  config.get("AZURE", "punjab_observation_data_blob_path")
   blob_service_client.create_blob_from_path(container_name=container_name, blob_name=blob_path, file_path=local_path)

# Remove the zip file
   os.remove(f"{zip_path}/{obs_name[1]}_{clock}.zip")
