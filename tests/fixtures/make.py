import json, pathlib
d = pathlib.Path(__file__).parent
# CMS JSON v2.0
j = {"hospital_name":"TEST GENERAL HOSPITAL","last_updated_on":"2026-01-15","version":"2.0.0",
 "hospital_location":["TEST GENERAL HOSPITAL"],"hospital_address":["1 Main St, Columbus, OH"],
 "license_information":{"license_number":"1234","state":"OH"},
 "standard_charge_information":[
  {"description":"MRI BRAIN W/O CONTRAST","code_information":[{"code":"70551","type":"CPT"}],
   "standard_charges":[{"gross_charge":3200.00,"discounted_cash":1900.00,"setting":"outpatient",
     "minimum":410.00,"maximum":2600.00,"billing_class":"facility",
     "payers_information":[
       {"payer_name":"Anthem Blue Cross Blue Shield","plan_name":"Commercial PPO","standard_charge_dollar":1450.00,"standard_charge_methodology":"fee schedule"},
       {"payer_name":"Aetna","plan_name":"Medicare Advantage HMO","standard_charge_dollar":610.00,"standard_charge_methodology":"case rate"},
       {"payer_name":"UnitedHealthcare","plan_name":"Choice Plus","standard_charge_percentage":45.0,"estimated_amount":1440.00,"standard_charge_methodology":"percent of total billed charges"},
       {"payer_name":"Payer 3","plan_name":"","standard_charge_dollar":999.00}]}]},
  {"description":"BASIC METABOLIC PANEL","code_information":[{"code":"80048","type":"CPT"}],
   "standard_charges":[{"gross_charge":120.0,"discounted_cash":60.0,"setting":"outpatient","minimum":9.5,"maximum":88.0,
     "payers_information":[{"payer_name":"Anthem Blue Cross Blue Shield","plan_name":"Commercial PPO","standard_charge_dollar":22.40,"standard_charge_methodology":"fee schedule"}]}]},
  {"description":"KNEE REPLACEMENT","code_information":[{"code":"470","type":"MS-DRG"}],
   "standard_charges":[{"gross_charge":78000.0,"setting":"inpatient","minimum":14000.0,"maximum":52000.0,
     "payers_information":[{"payer_name":"Anthem Blue Cross Blue Shield","plan_name":"Commercial PPO","standard_charge_dollar":31000.0,"standard_charge_methodology":"case rate"}]}]},
  {"description":"UNRELATED WIDGET","code_information":[{"code":"99999","type":"CPT"}],
   "standard_charges":[{"gross_charge":10.0,"setting":"outpatient","payers_information":[]}]}]}
(d/"cms_v2.json").write_text(json.dumps(j))

# CMS CSV tall, with the 2-row metadata preamble that breaks naive readers
tall = """hospital_name,last_updated_on,version,hospital_location,license_number|OH
TEST GENERAL HOSPITAL,2026-01-15,2.0.0,TEST GENERAL HOSPITAL,1234
description,code|1,code|1|type,billing_class,setting,standard_charge|gross,standard_charge|discounted_cash,payer_name,plan_name,standard_charge|negotiated_dollar,standard_charge|negotiated_percentage,estimated_amount,standard_charge|methodology,standard_charge|min,standard_charge|max
MRI BRAIN W/O,70551,CPT,facility,outpatient,3200,1900,Anthem BCBS,Commercial PPO,1450,,,fee schedule,410,2600
MRI BRAIN W/O,70551,CPT,facility,outpatient,3200,1900,Medical Mutual of Ohio,PPO,1180,,,fee schedule,410,2600
CBC W/ DIFF,85025,CPT,facility,outpatient,95,48,Anthem BCBS,Commercial PPO,17.30,,,fee schedule,7,66
WIDGET,99999,CPT,facility,outpatient,10,5,Anthem BCBS,Commercial PPO,4,,,fee schedule,1,9
"""
(d/"cms_tall.csv").write_text(tall)

wide = """description,code|1,code|1|type,setting,standard_charge|gross,standard_charge|discounted_cash,standard_charge|Anthem BCBS|Commercial PPO|negotiated_dollar,standard_charge|Aetna|MA HMO|negotiated_dollar,standard_charge|min,standard_charge|max
CT ABD PELVIS,74177,CPT,outpatient,4100,2400,1810,720,600,3300
"""
(d/"cms_wide.csv").write_text(wide)
print("fixtures written")
