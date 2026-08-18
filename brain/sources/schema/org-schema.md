# Salesforce Org Schema - Objects and Fields

API version 67.0 | 82 objects | 1884 fields

Format: `FieldApiName | Type | Label | extras`

---

# Custom Objects

## AI_Assignment_Logs__c - AI Assignment Logs (44 fields)

```
Candidate_Interview_Change__c | Lookup(Account) | Candidate Interview Change
Compass_R_scheduler_run_by__c | Lookup(Recruiter__c) | Compass R scheduler run by
Compass_R_scheduler_run_on__c | DateTime | Compass R scheduler run on
Compass_R_scheduler_run_Response_Body__c | LongTextArea | Compass R scheduler run Response Body
Compass_R_scheduler_run_Status_Code__c | Number | Compass R scheduler  run Status Code
Interview__c | Lookup(Interview__c) | Interview
Interview_ID__c | Lookup(Interview__c) | Interview ID
Interviews_ID__c | Number | Interviews ID
Last_Support_Person__c | Lookup(User) | Last Support Person
Last_Support_Person1__c | Lookup(Recruiter__c) | Last Support Person
Main_AI_Assignment_scheduler_run_by__c | Lookup(Recruiter__c) | Main AI Assignment scheduler run by
Main_AI_Assignment_scheduler_run_on__c | DateTime | Main AI Assignment scheduler run on
Main_AI_Assignment_scheduler_run_respons__c | LongTextArea | Main AI Assignment scheduler run respons
Main_AI_Assignment_scheduler_run_status__c | Number | Main AI Assignment scheduler run status
Manual_Main_AI_Assignment_Flow_run_by__c | Lookup(User) | Manual Main AI Assignment Flow run by
Manual_Main_AI_Assignment_run_by__c | Lookup(Recruiter__c) | Manual Main AI Assignment run by
Manual_Main_AI_Assignment_run_on__c | DateTime | Manual Main AI Assignment run on
Manual_Main_AI_Assignment_run_Response__c | LongTextArea | Manual Main AI Assignment run Response
Manual_Main_AI_Assignment_run_status_cod__c | Number | Manual Main AI Assignment run status cod
New_Support_Person__c | Lookup(Recruiter__c) | New Support Person
preferred_support_person_initiated_by__c | Lookup(User) | preferred support person initiated by
preferred_support_person_initiated_On__c | DateTime | preferred support person initiated On
Reassignment_by__c | Lookup(Recruiter__c) | Reassignment  by
Reassignment_Initiated_By__c | Lookup(User) | Reassignment Initiated By
Reassignment_on__c | DateTime | Reassignment on
Reassignment_Other_Reason__c | Text | Reassignment Other  Reason
Reassignment_Reason_Picklist_Values__c | Picklist | Reassignment Reason Picklist Values | values: Previous round extended for current Interview Support Person; Interview Support Person unavailable; Scheduling conflict; Interview Support Person at capacity; Skill / domain mismatch; Candidate preference; Time zone mismatch; Escalation from team lead / manager; Other
Reassignment_run_response_body__c | LongTextArea | Reassignment  run response body
Reassignment_run_status_code__c | Number | Reassignment  run status code
Reshuffle_Initiated_By__c | Lookup(User) | Reshuffle Initiated By
Reshuffle_Initiated_On__c | DateTime | Reshuffle Initiated On
Reshuffle_Reason__c | Picklist | Reshuffle Reason | values: Previous round extended for current Interview Support Person; Interview Support Person unavailable; Scheduling conflict; Interview Support Person at capacity; Skill / domain mismatch; Candidate preference; Time zone mismatch; Escalation from team lead / manager; Other
Reshuffle_Reason_Comment__c | Text | Reshuffle Reason Comment
Reshuffle_Run_Reason__c | Picklist | Reshuffle Run Reason | values: Previous round extended for current Interview Support Person; Interview Support Person unavailable; Scheduling conflict; Interview Support Person at capacity; Skill / domain mismatch; Candidate preference; Time zone mismatch; Escalation from team lead / manager; Other
Reshuffle_run_response_body__c | LongTextArea | Reshuffle run response body
reshuffle_run_status_code__c | Number | reshuffle run status code
Rollback_Initiated_By__c | Lookup(User) | Rollback Initiated By
Rollback_Response_Body__c | LongTextArea | Rollback Response Body
Rollback_Run_On__c | DateTime | Rollback Run On
Round__c | Text | Round
Round_Info__c | Text | Round Info
testing_field_for_jenkins__c | Text | testing field for jenkins
Total_Interview_Assigned__c | Number | Total Interview Assigned
Total_Interviews_Unassigned__c | Number | Total Interviews Unassigned
```

## Application__c - Application (10 fields)

```
Candidate__c | Lookup(Account) | Candidate
Date_of_Application__c | Date | Date of Application
Day_of_Application__c | Formula<Text> | Day of Application
Long_Applications__c | Number | Long Applications
Marketing__c | Lookup(Marketing__c) | Marketing
Reason_why_target_not_completed__c | Text | Reason why target not completed
Recruiter__c | Lookup(Recruiter__c) | Recruiter
Recruiter_s_Lead__c | Formula<Text> | Recruiter's Lead
Short_Applications__c | Number | Short Applications
Total_Applications__c | Formula<Number> | Total Applications
```

## Availability__c - Availability (10 fields)

```
Candidate__c | Lookup(Account) | Candidate
Date__c | Text | Date
Date_EST__c | Text | Date EST
End_DateTime__c | DateTime | End DateTime
End_Time__c | Text | End Time
End_Time_EST__c | Text | End Time EST
Start_DateTime__c | DateTime | Start DateTime
Start_Time__c | Text | Start Time
Start_Time_EST__c | Text | Start Time EST
Time_Zone__c | Picklist | Time Zone | required | globalValueSet: Candidate_Time_Zone
```

## Background_Check__c - Background Check (42 fields)

```
ACH_Authorization_Signed_Date__c | Formula<Date> | ACH Authorization Signed Date
ACH_Authorization_Status__c | Picklist | ACH Authorization Status | values: Pending; Signed
Assigned_to_QA__c | Checkbox | Accept this record from Queue
Background_Check_Project_Count__c | Summary | Background Check Project Count
Background_Check_Status__c | Picklist | Background Check Status | values: Payment Verification Pending; Documents Pending; Candidate Details Pending; Verification In Progress; Completed
Background_Check_Type__c | MultiselectPicklist | Verification Type | values: Background Verification; I9 Verification; Visa Assessment
Before_Background_Check__c | Checkbox | Before Background Check
C2C_Submission__c | Checkbox | C2C Submission
Candidate__c | Lookup(Account) | Candidate
Candidate_Details_Status__c | Picklist | Candidate Details Status | values: Pending; In Progress; Submitted
Company_Name__c | Formula<Text> | Company Name
DS_ACH_Debit_Date_Each_Month__c | Picklist | DS ACH Debit Date Each Month | values: 1; 2; 3; 4; 5; 6; 7; 8; 9; 10; 11; 12; 13; 14; 15; 16; 17; 18; 19; 20; 21; 22; 23; 24; 25; 26; 27; 28; 29; 30
DS_Debit_Amount__c | Currency | DS Debit Amount
DS_Debit_Amount_Formate__c | Formula<Text> | DS Debit Amount Formate
DS_Final_Payment_Due_Date__c | Date | DS Final Payment Due Date
DS_Final_Payment_Due_Date_Formate__c | Formula<Text> | DS Final Payment Due Date Formate
DS_First_Installment_Due_Date__c | Date | DS First Installment Due Date
DS_First_Installment_Due_Date_Formatted__c | Formula<Text> | DS First Installment Due Date Formatted
DS_Payment_Commencement_Date__c | Date | DS Payment Commencement Date
DS_Payment_Commencement_Date_Formatted__c | Formula<Text> | DS Payment Commencement Date Formatted
DS_Payment_Period_Month__c | Picklist | DS Payment Period (Month) | values: 1; 2; 3; 4; 5; 6; 7; 8; 9; 10; 11; 12
DS_Total_Amount_to_be_Paid__c | Currency | DS Total Amount to be Paid
DS_Total_Amount_to_be_Paid_Formate__c | Formula<Text> | DS Total Amount to be Paid Formate
Final_Employer_Confirmation_Date__c | Formula<Date> | Final Employer Confirmation  Date
Final_Employer_Confirmation_Received__c | Checkbox | Final Employer Confirmation Received
Interview__c | Lookup(Interview__c) | Interview
Interview_Round__c | Text | Interview Round
Last_Notification_Sent__c | DateTime | Last Notification Sent
Notification_Active__c | Checkbox | Notification Active
Offer_Letter_Payment_Received_Date__c | Formula<Date> | Offer Letter Payment Received Date
Offer_Letter_Payment_Status__c | Picklist | Offer Letter Payment Status | values: Pending; Received
Pending_Items_Summary__c | TextArea | Pending Items Summary
Promissory_Note_Signed_Date__c | Formula<Date> | Promissory Note Signed Date
Promissory_Note_Status__c | Picklist | Promissory Note Status | values: Pending; Signed
QA_Notes__c | LongTextArea | QA Notes
Reference_Check_Completed__c | Checkbox | Reference Check Completed
Send_Pending_Notifications__c | Checkbox | Received Background Notifications
Session__c | Lookup(Session__c) | Session
Stage__c | Picklist | Stage | values: Reference Check; Offer Received
Stage_Detail__c | Formula<Text> | Stage Detail
Summary__c | Formula<Text> | Summary
Verification_Status__c | Picklist | Verification Status | values: Not Started; In Progress; Completed
```

## Background_Check_Employment__c - Background Check Employment (21 fields)

```
Background_Check__c | MasterDetail(Background_Check__c) | Background Check
Employer_Confirmed__c | Checkbox | Employer Confirmed
Employer_Name__c | Text | Employer Name
End_Date__c | Date | End Date
Present__c | Checkbox | Current Employer
Project_Name__c | Text | Project Name
Ref_1_Designation__c | Text | Ref 1 Designation
Ref_1_Email__c | Email | Ref 1 Email
Ref_1_Name__c | Text | Ref 1 Name
Ref_1_Phone__c | Phone | Ref 1 Phone
Ref_2_Designation__c | Text | Ref 2 Designation
Ref_2_Email__c | Email | Ref 2 Email
Ref_2_Name__c | Text | Ref 2 Name
Ref_2_Phone__c | Phone | Ref 2 Phone
Ref_3_Designation__c | Text | Ref 3 Designation
Ref_3_Email__c | Email | Ref 3 Email
Ref_3_Name__c | Text | Ref 3 Name
Ref_3_Phone__c | Phone | Ref 3 Phone
Start_Date__c | Date | Start Date
Verification_Notes__c | LongTextArea | Verification Notes
Verification_Status__c | Picklist | Verification Status | values: Not Started; In Progress; Verified; Unable to Verify
```

## Candidate_Email__c - Candidate Email (6 fields)

```
Body__c | Html | Body
Candidate_Email_Id__c | Text | Candidate Email Id
Description__c | LongTextArea | Description
Email_Classification__c | Text | Email Classification
Sender_Email_Id__c | Text | Sender Email_Id
Subject__c | Text | Subject
```

## Candidate_Portal_Credential__c - Candidate Portal Credential (7 fields)

```
Candidate__c | Lookup(Account) | Candidate
Failed_Login_Count__c | Number | Failed Login Count
Is_Active__c | Checkbox | Is Active
Last_Login_At__c | DateTime | Last Login At
Locked_Until__c | DateTime | Locked Until
Password__c | Text | Password | required
Username__c | Text | Username | required | unique
```

## Candidate_Portal_Session__c - Candidate Portal Session (7 fields)

```
Candidate_Credential__c | Lookup(Candidate_Portal_Credential__c) | Candidate Credential
Created_At__c | DateTime | Created At
Expires_At__c | DateTime | Expires At
IP_Address__c | Text | IP Address
Is_Active__c | Checkbox | Is Active
Last_Activity_At__c | DateTime | Last Activity At
Session_Id__c | Text | Session Id | required | unique | externalId
```

## Candidate_Training__c - Candidate Training (22 fields)

```
Assigned_Trainer__c | Lookup(Recruiter__c) | Assigned Trainer
Assigned_Trainer_User__c | Lookup(User) | Assigned Trainer User
Candidate__c | Lookup(Account) | Candidate | required
Candidate_Email__c | Email | Candidate Email
Candidate_Feedback__c | LongTextArea | Candidate Feedback
Candidate_Owner__c | Lookup(User) | Candidate Owner
Candidate_Time_Zone_End_Time__c | Picklist | Candidate Time Zone End Time | globalValueSet: Times
Candidate_Time_Zone_Start_Time__c | Picklist | Candidate Time Zone Start Time | globalValueSet: Times
Cohort__c | Lookup(Cohort__c) | Cohort
Drop_Date__c | Date | Drop Date
End_Date__c | Date | End Date
End_Time_IST__c | Text | End Time IST
Niche__c | Picklist | Niche | values: AI ML Engineer; Python Developer/Engineer; Java Fullstack Developer/Engineer; Product/Project Manager; Marketing Manager; Finance Analyst; Other
Program__c | Lookup(Program__c) | Program
Program_Version__c | Lookup(Program_Version__c) | Program Version | required
Session_Window__c | Lookup(Session_Window__c) | Session Window
Start_Date__c | Date | Start Date
Start_Tme_IST__c | Text | Start Tme IST
Status__c | Picklist | Status | required | values: Planned; Active; Paused; Completed; Dropped
Time_Zone__c | Picklist | Time Zone | globalValueSet: Candidate_Time_Zone
Trainer_Feedback__c | LongTextArea | Trainer Feedback
Whatsapp_No__c | Text | Whatsapp No
```

## CandidateTrainingStep__c - Candidate Training Step (16 fields)

```
Assigned_Trainer__c | Lookup(Recruiter__c) | Assigned Trainer
Assigned_Trainer_Name__c | Formula<Text> | Assigned Trainer Name
Assigned_Trainer_User__c | Lookup(User) | Assigned Trainer User
Candidate_Training__c | MasterDetail(Candidate_Training__c) | Candidate Training
Completed__c | DateTime | Completed
Due_At__c | DateTime | Due At
Duration__c | Number | Duration
Expected_Duration__c | Number | Expected Duration
Learning_Goal__c | LongTextArea | Learning Goal
Notes__c | LongTextArea | Notes
Sequence__c | Number | Sequence
Started_At__c | DateTime | Started At
Status__c | Picklist | Status | values: Not Started; In Progress; Blocked; Skipped; Completed; Absent; Dropped
Step_Template__c | Lookup(StepTemplate__c) | Step Template
Training_Method__c | LongTextArea | Training Notes
Training_Step__c | LongTextArea | Training Step
```

## Cohort__c - Cohort (4 fields)

```
Description__c | LongTextArea | Description
End_Date__c | Date | End Date
Lead_Trainer__c | Lookup(User) | Lead_Trainer
Start_Date__c | Date | Start Date
```

## Company__c - Company (4 fields)

```
Company_Website__c | Url | Company Website
Interview_Difficulty__c | Formula<Text> | Interview Difficulty
Tier__c | Picklist | Tier | values: FAANG / Top Product; Product Company; Startup; Services / Consulting; Mid-Market; Enterprise; Other
Typical_Rounds__c | Number | Typical Rounds
```

## Course__c - Course (1 fields)

```
Active__c | Checkbox | Active
```

## Deliverable__c - Deliverable (21 fields)

```
AI_Latest_Score__c | Text | AI Latest Score
Approval_Required__c | Checkbox | Approval Required
Approved_At__c | DateTime | Approved At
Approved_By__c | Lookup(User) | Approved By
Attempt_Count__c | Number | Attempt Count
Candidate_Training__c | Lookup(Candidate_Training__c) | Candidate Training
Candidate_Training_Step__c | MasterDetail(CandidateTrainingStep__c) | Candidate Training Step
Days_To_Complete__c | Number | Days To Complete
Definitions__c | Lookup(Step_Deliverable_Definition__c) | Definition
Due_Date_Candidate_Time_Zone__c | Text | Due Date Candidate Time Zone
Due_Date_IST__c | Text | Due Date IST
Due_Date_Sort__c | DateTime | Due Date Sort
Extension_Requested__c | Checkbox | Extension Requested
Extension_Requested_At__c | DateTime | Extension Requested At
Is_Required__c | Checkbox | Is Required
Latest_AI_Feedback__c | LongTextArea | Latest AI Feedback
Linked_Session__c | Lookup(Session__c) | Linked Session
Notes__c | LongTextArea | Notes
Status__c | Picklist | Status | values: Inprogress; Not Active; Pending; Submitted; Failed; Locked; Approved; Active; Dropped
Submitted_At__c | DateTime | Submitted At
Submitted_By__c | Lookup(User) | Submitted By
```

## Deliverable_Result__c - Deliverable Result (7 fields)

```
AI_Feedback__c | LongTextArea | AI Feedback
AI_Score__c | Number | AI Score
Deliverable__c | MasterDetail(Deliverable__c) | Deliverable
Deliverable_Path__c | Text | Deliverable Path
Result_Status__c | Picklist | Result Status | values: Pass; Fail; Pending
Submitted_At__c | DateTime | Submitted At
Threshold_Score__c | Text | Threshold Score
```

## Department__c - Department (0 fields)

_No retrievable fields._

## Education_History__c - Education History (12 fields)

```
Candidate__c | Lookup(Account) | Candidate
CGPA_Score__c | Number | CGPA / Score
Course__c | Lookup(Course__c) | Course
Degree__c | Picklist | Degree | globalValueSet: Degree
Degree_Type__c | Picklist | Degree Type | globalValueSet: Degree_Type
End_Date__c | Date | End Date
Institute__c | Lookup(Institute__c) | Institute
Institute_Country__c | Picklist | Institute Country | globalValueSet: Country
Institute_Name__c | Formula<Text> | Institute Name
Lead__c | Lookup(Lead) | Lead
Onboarding__c | Lookup(Onboarding__c) | Onboarding
Start_Date__c | Date | Start Date
```

## Employee_Leave__c - Employee Leave (41 fields)

```
Attachment_Required__c | Checkbox | Attachment Required
Cancel_Reason__c | LongTextArea | Cancel Reason
Employee__c | Lookup(Recruiter__c) | Employee
Employees__c | Lookup(User) | Employeess
End_Date__c | Date | End Date
End_DateTime__c | DateTime | End DateTime
EndTime__c | Picklist | End Time | values: 12:00 AM; 12:15 AM; 12:30 AM; 12:45 AM; 01:00 AM; 01:15 AM; 01:30 AM; 01:45 AM; 02:00 AM; 02:15 AM; 02:30 AM; 02:45 AM; 03:00 AM; 03:15 AM; 03:30 AM; 03:45 AM; 04:00 AM; 04:15 AM; 04:30 AM; 04:45 AM; 05:00 AM; 05:15 AM; 05:30 AM; 05:45 AM; 06:00 AM; 06:15 AM; 06:30 AM; 06:45 AM; 07:00 AM; 07:15 AM; 07:30 AM; 07:45 AM; 08:00 AM; 08:15 AM; 08:30 AM; 08:45 AM; 09:00 AM; 09:15 AM; 09:30 AM; 09:45 AM; 10:00 AM; 10:15 AM; 10:30 AM; 10:45 AM; 11:00 AM; 11:15 AM; 11:30 AM; 11:45 AM; 12:00 PM; 12:15 PM; 12:30 PM; 12:45 PM; 01:00 PM; 01:15 PM; 01:30 PM; 01:45 PM; 02:00 PM; 02:15 PM; 02:30 PM; 02:45 PM; 03:00 PM; 03:15 PM; 03:30 PM; 03:45 PM; 04:00 PM; 04:15 PM; 04:30 PM; 04:45 PM; 05:00 PM; 05:15 PM; 05:30 PM; 05:45 PM; 06:00 PM; 06:15 PM; 06:30 PM; 06:45 PM; 07:00 PM; 07:15 PM; 07:30 PM; 07:45 PM; 08:00 PM; 08:15 PM; 08:30 PM; 08:45 PM; 09:00 PM; 09:15 PM; 09:30 PM; 09:45 PM; 10:00 PM; 10:15 PM; 10:30 PM; 10:45 PM; 11:00 PM; 11:15 PM; 11:30 PM; 11:45 PM
From_Session__c | Picklist | From Session | values: First Half; Second Half; Full Day
HR_Approved_On__c | DateTime | HR Approved On
HR_Approver__c | Lookup(Recruiter__c) | HR Approver
HR_Requirement_Reason__c | Picklist | HR Requirement Reason | values: Late Request; Insufficient Balance; Special Leave; Comp-Off; Other
Is_Full_Day__c | Checkbox | Is Full Day
Is_Half_Day__c | Checkbox | Is Half Day
Is_HR_Approval_Required__c | Checkbox | Is HR Approval Required
Is_Unpaid__c | Checkbox | Is Unpaid
Leave_Applied_By__c | Lookup(Recruiter__c) | Leave Applied By
Leave_Event_Id__c | Text | Leave Event Id
Leave_Reason__c | LongTextArea | Leave Reason
Leave_Type__c | Picklist | Leave Type | values: PTO; Sick; Unpaid (LOP/LWP); Comp-Off Availed; Marriage; Paternity; Maternity; Other
Leave_Year__c | Number | Leave Year
Manager_Approved_On__c | DateTime | Manager Approved On
Manager_Approver__c | Lookup(Employee) | Manager Approver
Notice_Compliance__c | Picklist | Notice Compliance | values: On Time; Late; Not Applicable
Notice_Given_Days__c | Number | Notice Given (Days)
Notice_Required_Days__c | Number | Notice Requirement (Days)
Paid_Days__c | Number | Paid Days
Partial_Day_Type__c | Picklist | Partial Day Type | values: First Half; Second Half; Time Range
PTO_Balance_At_Request__c | Number | Balance Snapshot (PTO)
Rejection_Reason__c | LongTextArea | Rejection Reason
Requested_By__c | Lookup(Employee) | Requested By
Requested_On__c | DateTime | Requested On
SL_Balance_At_Request__c | Number | Balance Snapshot (SL)
Start_Date__c | Date | Start Date
Start_DateTime__c | DateTime | Start DateTime
Start_Time__c | Time | Start Time
StartTime__c | Picklist | Start Time | values: 12:00 AM; 12:15 AM; 12:30 AM; 12:45 AM; 01:00 AM; 01:15 AM; 01:30 AM; 01:45 AM; 02:00 AM; 02:15 AM; 02:30 AM; 02:45 AM; 03:00 AM; 03:15 AM; 03:30 AM; 03:45 AM; 04:00 AM; 04:15 AM; 04:30 AM; 04:45 AM; 05:00 AM; 05:15 AM; 05:30 AM; 05:45 AM; 06:00 AM; 06:15 AM; 06:30 AM; 06:45 AM; 07:00 AM; 07:15 AM; 07:30 AM; 07:45 AM; 08:00 AM; 08:15 AM; 08:30 AM; 08:45 AM; 09:00 AM; 09:15 AM; 09:30 AM; 09:45 AM; 10:00 AM; 10:15 AM; 10:30 AM; 10:45 AM; 11:00 AM; 11:15 AM; 11:30 AM; 11:45 AM; 12:00 PM; 12:15 PM; 12:30 PM; 12:45 PM; 01:00 PM; 01:15 PM; 01:30 PM; 01:45 PM; 02:00 PM; 02:15 PM; 02:30 PM; 02:45 PM; 03:00 PM; 03:15 PM; 03:30 PM; 03:45 PM; 04:00 PM; 04:15 PM; 04:30 PM; 04:45 PM; 05:00 PM; 05:15 PM; 05:30 PM; 05:45 PM; 06:00 PM; 06:15 PM; 06:30 PM; 06:45 PM; 07:00 PM; 07:15 PM; 07:30 PM; 07:45 PM; 08:00 PM; 08:15 PM; 08:30 PM; 08:45 PM; 09:00 PM; 09:15 PM; 09:30 PM; 09:45 PM; 10:00 PM; 10:15 PM; 10:30 PM; 10:45 PM; 11:00 PM; 11:15 PM; 11:30 PM; 11:45 PM
Status__c | Picklist | Status | values: Planned; Approved; Cancelled; Completed; Draft; Submitted; Manager Approved; HR Review Required; Rejected
Supporting_Document_Link__c | Url | Supporting Document
To_Session__c | Picklist | To Session | values: First Half; Second Half; Full Day
Total_Days__c | Number | Total Days Requested
Unpaid_Days__c | Number | Unpaid Days (LOP)
```

## In_App_Checklist_Settings__c - In App Checklist Settings (2 fields)

```
ProfileKey__c | Text | ProfileKey
Sales_Cloud_In_App_Page__c | Url | Sales Cloud In App Page
```

## Institute__c - Institute (2 fields)

```
Active__c | Checkbox | Active
Country__c | Picklist | Country | globalValueSet: Country
```

## Internal_Interview__c - Internal Interview (61 fields)

```
AI_Assignment_Confidence__c | Percent | AI Assignment Confidence
AI_Assignment_Reason__c | LongTextArea | AI Assignment Reason
AI_Audio_Analysis__c | LongTextArea | AI Audio Analysis
AI_Decision__c | Picklist | AI Decision | values: Hire; No Hire
AI_Pregen_Status__c | Text | AI Pregen Status
AI_Pregenerated_Questions__c | LongTextArea | AI Pregenerated Questions
AI_Total_score__c | Number | AI Total score
AI_Video_Analysis__c | LongTextArea | AI Video Analysis
Assignment_Reason__c | LongTextArea | Assignment Reason
Candidate__c | Lookup(Account) | Candidate
Candidate_Lacking_IR__c | MultiselectPicklist | Candidate Lacking (IR) | globalValueSet: Evaluation_Parameter
Candidate_Lacking_IS__c | MultiselectPicklist | Candidate Lacking (IS) | globalValueSet: Evaluation_Parameter
Candidate_Training__c | Lookup(Candidate_Training__c) | Candidate Training
Combined_Score__c | Number | Combined Score
Comment_Summary__c | LongTextArea | Comment Summary
Company__c | Lookup(Company__c) | Company
Date__c | Date | Scheduled Date
Deadline__c | Date | Result Deadline
Employement_Type__c | Picklist | Employement Type | values: B2B; C2C; W2 Contract; Full Time
End_Time_IST__c | Text | End Time IST
Evaluation_Parameter__c | MultiselectPicklist | Evaluation  Parameter | globalValueSet: Evaluation_Parameter
Final_Decision_Date__c | DateTime | Final Decision Date
Final_Descion__c | Picklist | Final Decision | values: Hire; NoHire
Has_Resume__c | Checkbox | Has Resume
Hired_Niche__c | Lookup(Niche__c) | Hired Niche
Human_Decision__c | Picklist | Human Decision | values: Hire; NoHire; Retake; Pass; Fail; Needs Improvement
Human_Total_Score__c | Number | Human Total Score
Interview_History_Notes__c | Html | Interview History & Notes
Interview_Support_Person__c | Lookup(Recruiter__c) | Interview Support Person
Interview_Type__c | Lookup(Interview_Type__c) | Interview Type
Interviewer__c | Lookup(Recruiter__c) | Interviewer
IR_Feedback__c | LongTextArea | IR Feedback
IS_Feedback__c | LongTextArea | IS Feedback
Is_Final_Week_Mock__c | Checkbox | Is Final Week Mock
IS_Improvement_Feedback__c | LongTextArea | IS Improvement  Feedback
JD_Link__c | LongTextArea | JD Link
Last_Result_Reminder_At__c | DateTime | Last Result Reminder At
Launch_mode__c | Picklist | Launch mode | values: Techsara's Magic; Template
Lead__c | Lookup(Lead) | Lead
Mock_Status__c | Picklist | Mock Status | values: Active; Unassigned; Resolved
Niche__c | Lookup(Niche__c) | Niche
Postion__c | Text | Position
Previous_internal_interview__c | Lookup(Internal_Interview__c) | Previous internal interview
Result_Reminder_Count__c | Number | Result Reminder Count
Round__c | Picklist | Round | globalValueSet: Round
Round_Info__c | Picklist | Round Info | globalValueSet: Round_Info
Round_Rank__c | Number | Round Rank
Round_ranking_reason__c | LongTextArea | Round ranking reason
Scheduled_Date__c | Date | Date Of Interview
Selected_Time_Zone_End_Time__c | Picklist | Selected Time Zone End  Time | globalValueSet: Times
Selected_Time_Zone_Start_Tim__c | Picklist | Selected Time Zone Start Time | globalValueSet: Times
Session__c | Lookup(Session__c) | Session
Start_Time_IST__c | Text | Start Time IST
Status__c | Picklist | Status | values: Unscheduled; Scheduled; Rescheduled; Cancelled; Completed
Support_Block_End__c | DateTime | Support Block End
Support_Block_Start__c | DateTime | Support Block Start
Support_Notes__c | LongTextArea | Support Notes
Template__c | Lookup(Template__c) | Template
Time_Zone__c | Picklist | Time Zone | globalValueSet: Candidate_Time_Zone
Triggered_By_Step__c | Lookup(CandidateTrainingStep__c) | Triggered By Step
Week_Number__c | Number | Week Number
```

## Internal_Interview_Question_Log__c - Internal Interview Question Log (11 fields)

```
Evalation_Criteria_log__c | LongTextArea | Evalation Criteria log
Follow_Ups_Log__c | LongTextArea | Follow Ups Log
Internal_Interview__c | MasterDetail(Internal_Interview__c) | Internal Interview
Internal_Interview_Section_Log__c | Lookup(Internal_Interview_Section_Log__c) | Internal Interview Section Log
Notebook_Link_Log__c | Url | Notebook Link Log
Question_Bank__c | Lookup(Question_Bank__c) | Question Bank
Question_Source_Used__c | Picklist | Question Source Used | values: Standard; AI Resume Based
Question_Text__c | LongTextArea | Question Text
Scenario_Log__c | LongTextArea | Scenario Log
Section__c | Lookup(Section__c) | Section
Sequence__c | Number | Sequence
```

## Internal_Interview_Section_Log__c - Internal Interview Section Log (7 fields)

```
Error_Log__c | LongTextArea | Error Log
Internal_Interview__c | MasterDetail(Internal_Interview__c) | Internal Interview
Question_Source_Used__c | Picklist | Question Source Used | values: Standard; AI Resume Based
Section__c | Lookup(Section__c) | Section
Section_Comment__c | LongTextArea | Section Comment
Section_Score__c | Number | Section Score
Sequence__c | Number | Sequence
```

## Interview__c - Interview (257 fields)

```
Account_Manager__c | Lookup(User) | Account Manager
Advanced_to_Next_Round__c | Picklist | Advanced to Next Round? | values: Yes; No
Affinity_A__c | Number | Affinity A
AI_Assignment_Confidence__c | Percent | AI Assignment Confidence
AI_Assignment_Reason__c | LongTextArea | AI Assignment Reason
AI_Assignment_Version__c | Text | AI Assignment Version
AI_Suggestion_JSON__c | LongTextArea | AI Suggestion JSON
AI_Suggestion_Status__c | Picklist | AI Suggestion Status | values: Not Requested; Requested; Available; Used; Expired; Error.
AI_Suggestions_Expiry__c | DateTime | AI Suggestions Expiry
AI_Suggestions_Generated_On__c | DateTime | AI Suggestions Generated On
Alpha_Used__c | Number | Alpha Used
Applicant_Email__c | Email | Applicant Email
Application__c | Lookup(Application__c) | Application
Approve_Stage__c | Picklist | Approve Stage | values: Ready; Submitted; Assigned; Rejected; Completed
Assigned_By__c | Picklist | Assigned By | values: AI Nightly; AI Suggestion; Manual; Extended Reassign; Other
Assignment_Error_Message__c | LongTextArea | Assignment Error Message
Assignment_Priority__c | Picklist | Assignment Priority | values: Low; Medium; High; Critical
Assignment_Reason__c | LongTextArea | Assignment Reason
Assignment_Status__c | Picklist | Assignment Status | values: Not Assigned; Assigned; No Bandwidth; Needs Manual Review; Assignment Failed; Reassigned Due To Extension
Base_Credit__c | Formula<Number> | Base Credit
Base_Magnitude_B__c | Number | Base Magnitude B
Calendar_Event_Id__c | Text | Calendar Event Id
Calendar_Status__c | Text | Calendar Status
Call_Type__c | Picklist | Call Type | values: Zoom video call; Google meet video call; Microsoft Teams Video Call; Normal phone call; Other
Candidate__c | MasterDetail(Account) | Candidate
Candidate_Feedback__c | LongTextArea | Candidate Feedback
Candidate_Name__c | Text | Candidate Name
Candidate_Name_For__c | Formula<Text> | Candidate Name
Candidate_Phone_Number__c | Formula<Text> | Candidate Phone Number
Candidate_Rate__c | Currency | Candidate Rate
Candidate_Status__c | Formula<Text> | Candidate Status
Cap_Hit__c | Checkbox | Cap Hit
Cap_Limit__c | Number | Cap Limit
Channel_Thread_Timestamp__c | Text | Channel Thread Timestamp
Client_Account__c | Lookup(Account) | Client Account
Client_Feedback__c | LongTextArea | Client Feedback
Client_Feedback_Status__c | Picklist | Client Feedback Status | values: Pending; Received
Client_Lead__c | Lookup(Lead) | Client Lead
Coherence_Factor_Fc__c | Number | Coherence Factor Fc
Coherence_Phi__c | Number | Coherence Phi
Combined_F__c | Number | Combined F
Company__c | Text | Company(Do Not Use)
Company_Name__c | Formula<Text> | Company Name
company_new__c | Lookup(Company__c) | Company
Credit_Scale_C__c | Number | Credit Scale C
Current_Record_Link__c | Formula<Text> | Current Record Link
Curvature_Kappa__c | Number | Curvature Kappa
Cutoff_Date__c | Formula<Date> | Cutoff Date
Date_of_Interview__c | Date | Date of Interview
Delta_Accepted__c | Number | Delta Accepted
Delta_Combined__c | Number | Delta Combined
Delta_Final__c | Number | Delta Final
Delta_L__c | Number | Delta L
Delta_NL__c | Number | Delta NL
Delta_Patterned__c | Number | Delta Patterned
Delta_Post_MFRM__c | Number | Delta Post MFRM
Depth_Weight_W__c | Number | Depth Weight W
Description__c | LongTextArea | Description
Duration_Signal_D_rel__c | Number | Duration Signal D rel
Duration_Weight_D_abs__c | Number | Duration Weight D abs
Employment_Type__c | Picklist | Employment Type | values: C2C; W2 Contract; B2B; Contract; Contract-to-Hire; Full Time
End_Time__c | Time | End Time(Do Not Use)
End_Time_Text__c | Formula<Text> | End Time Text
EndTime__c | Picklist | End Time | globalValueSet: Times
Experience_Mu__c | Number | Experience Mu
Final_Notification_Date_Time__c | DateTime | Final Notification Date/Time
Follow_Up_Active__c | Checkbox | Follow-Up Active
Follow_Up_Completed_By__c | Lookup(User) | Follow-Up Completed By
Follow_Up_Completed_Date__c | DateTime | Follow-Up Completed Date
Follow_Up_Completion_Reason__c | Picklist | Follow-Up Completion Reason | values: Feedback Received; Final Outcome Updated; No Response After Three Reminders; Candidate Withdrew; Position Closed; Interview Cancelled; Company Requested No Further Follow-Up; Follow-Up Not Required; Other
Follow_Up_Method__c | Picklist | Follow-Up Method | values: Email; Phone Call; Meeting; Messaging Platform; Other
Follow_Up_Notes__c | LongTextArea | Follow-Up Notes
Follow_Up_Status__c | Picklist | Follow-Up Status | values: Pending; In Progress; Completed; Paused; Not Required
G_Factor__c | Number | G Factor
Gamma_R__c | Number | Gamma R
Google_Calendar_Id__c | Text | Google Calendar Id
Google_Docs_ID__c | LongTextArea | Google Docs ID
Google_Docs_URL__c | Url | Google Docs URL
Google_Event_Url__c | TextArea | Google Event Url
If_yes_why__c | LongTextArea | If yes why?
Importance_Score_I__c | Number | Importance Score I
Incentive_Amount__c | Number | Incentive Amount
Incentive_Amount_Formula__c | Formula<Number> | Incentive Amount
Incentive_Calculated_On__c | DateTime | Incentive Calculated On
Incentive_eligiblle__c | Checkbox | Incentive eligiblle?
Incentive_Rate_Applied__c | Currency | Incentive Rate Applied
Incentive_Rate_Applied_Formula__c | Formula<Number> | Incentive Rate Applied
Incentive_Type__c | Picklist | Incentive Type | values: Positive; Negative; None
Incentive_Type_Formula__c | Formula<Text> | Incentive Type
Innovation_Epsilon__c | Number | Innovation Epsilon
Innovation_Variance__c | Number | Innovation Variance
Innovation_Window_N__c | Number | Innovation Window N
Interview_After_Application_Start_Date__c | Formula<Checkbox> | Interview After Application Start Date
Interview_Meeting_Link__c | Url | Interview Meeting Link
Interview_Mode__c | Picklist | Interview Mode | values: Audio; Onsite; Video
Interview_Month__c | Formula<Date> | Interview Month
Interview_Notes__c | LongTextArea | Interview Notes
Interview_Outcome__c | Picklist | Interview Outcome | values: Moved to Next Round; Rejected; Ghosted; Reference Check; Offer Received; NA
Interview_Questions__c | LongTextArea | Interview Questions
Interview_Recording_Key__c | Text | Interview Recording Key
Interview_Recording_Link__c | LongTextArea | Interview Recording Link
Interview_Source__c | Picklist | Interview Source | values: B2B Requirement; Candidate Marketing; Other
Interview_Status__c | Picklist | Interview Status | values: Scheduled; Completed; Rescheduled; Cancelled; No Show
Interview_Support_Person__c | Lookup(Recruiter__c) | Interview Support Person
Interview_Support_provided__c | Checkbox | Interview Support provided?
Interview_Time_Zone__c | Picklist | Interview Time Zone | values: ET (Eastern Time); CT (Central Time); MT (Mountain Time); PT (Pacific Time); AT (Alaska Time); HT (Hawaii Time); UTC (Coordinated Universal Time)
Interview_Type__c | Picklist | Interview Type | values: Screening Round; Technical Round; HR Round; Interview
Interviewer_Assignment_Constraint__c | Lookup(Recruiter__c) | Interviewer  Assignment Constraint
Interviewer_s_Email__c | Text | Interviewer Email
Interviewer_s_Name__c | Text | Interviewer Name
Interviewer_Severity_Cj__c | Number | Interviewer Severity Cj
Is_Current_Month__c | Formula<Checkbox> | Is_Current_Month
Is_Lead_Populated__c | Formula<Checkbox> | Is Lead Populated
Is_Loop_Round__c | Checkbox | Is Loop Round?
IsInterview__c | Formula<Number> | IsInterview
IST_End_DateTime__c | Formula<DateTime> | IST End DateTime
IST_End_Time__c | Text | IST End Time
IST_Start_DateTime__c | Formula<DateTime> | IST Start DateTime
IST_Start_Time__c | Text | IST Start Time
IsValidated__c | Checkbox | Bypass Ghosted Validation
JD_Link__c | Url | JD Link(Do Not Use)
JD_Link_url__c | LongTextArea | JD Link
Job_Description__c | LongTextArea | Job Description
Job_Requirement__c | Lookup(Job_Requirement__c) | Job Requirement
Job_Submission__c | Lookup(Job_Submission__c) | Job Submission
Kalman_Gain_K__c | Number | Kalman Gain K
Kalman_P_JSON__c | LongTextArea | Kalman P JSON
Kalman_P_Posterior__c | Number | Kalman P Posterior
Kalman_P_Prior__c | Number | Kalman P Prior
Kalman_R_Adaptive__c | Number | Kalman R Adaptive
L10_Score_After__c | Number | L10 Score After
L10_Score_Before__c | Number | L10 Score Before
Lambda_Trust__c | Number | Lambda Trust
Last_Follow_Up_Date__c | DateTime | Last Follow-Up Date
Last_Reminder_Sent_Date__c | DateTime | Last Reminder Sent Date
Lead__c | Lookup(Lead) | Lead
Manager_Thread_Timestamp__c | Text | Manager Thread Timestamp
Manual_Follow_Up_Count__c | Number | Manual Follow-Up Count
Marketing__c | Lookup(Marketing__c) | Marketing
Marketing_Unique_Interview_Key__c | Formula<Text> | Marketing Unique Interview Key
MFRM_Multiplier__c | Number | MFRM Multiplier
MFRM_Severity_Cj__c | Number | MFRM Severity Cj
Momentum_EMA__c | Number | Momentum EMA
Momentum_Factor_Fm__c | Number | Momentum Factor Fm
Net_Delta_AIML__c | Number | Net Delta AIML
Net_Delta_Backend__c | Number | Net Delta Backend
Net_Delta_FinalRnd__c | Number | Net Delta FinalRnd
Net_Delta_Frontend__c | Number | Net Delta Frontend
Net_Delta_Leetcode__c | Number | Net Delta Leetcode
Net_Delta_Overall__c | Number | Net Delta Overall
Net_Delta_SysDesign__c | Number | Net Delta SysDesign
Next_Reminder_Date__c | DateTime | Next Reminder Date
Offer_Letter_Date__c | Date | Offer Letter Date
Plasticity_Pi__c | Number | Plasticity Pi
Position__c | Text | Position
Preferred_Support_Person__c | Lookup(Employee) | Preferred Support Person
Previous_Support_Person__c | Lookup(Employee) | Previous Support Person
Project_Understanding_Document__c | LongTextArea | Project Understanding Document
Protective_Dampening_Rho__c | Number | Protective Dampening Rho
Reasoning__c | LongTextArea | Reasoning
Reassigned_Due_To_Extension__c | Checkbox | Reassigned Due To Extension
Reassignment_Initiated_By__c | Lookup(User) | Reassignment Initiated By
Reassignment_Initiated_On__c | DateTime | Reassignment Initiated On
Reassignment_Reason__c | Picklist | Reassignment Reason | values: Previous round extended for current Interview Support Person; Interview Support Person unavailable; Scheduling conflict; Interview Support Person at capacity; Skill / domain mismatch; Candidate preference; Time zone mismatch; Escalation from team lead / manager; Other
Reassignment_Reason_Comment__c | LongTextArea | Reassignment Reason Comment
Recruiter__c | Lookup(Recruiter__c) | Recruiter
Recruiter_Name__c | Text | Recruiter Name
Recruiter_Name_formula__c | Formula<Text> | Recruiter Name
Recruiter_s_Email__c | Text | Recruiter's Email (Company)
Recruiter_s_Name__c | Text | Recruiter's Name (Company)
Ref_1_Designation__c | Text | Ref 1 Designation
Ref_1_Email__c | Email | Ref 1 Email
Ref_1_Name__c | Text | Ref 1 Name
Ref_1_Notified__c | Checkbox | Ref 1 Notified
Ref_1_Phone__c | Phone | Ref 1 Phone
Ref_2_Designation__c | Text | Ref 2 Designation
Ref_2_Email__c | Email | Ref 2 Email
Ref_2_Name__c | Text | Ref 2 Name
Ref_2_Notified__c | Checkbox | Ref 2 Notified
Ref_2_Phone__c | Phone | Ref 2 Phone
Ref_3_Designation__c | Text | Ref 3 Designation
Ref_3_Email__c | Email | Ref 3 Email
Ref_3_Name__c | Text | Ref 3 Name
Ref_3_Notified__c | Checkbox | Ref 3 Notified
Ref_3_Phone__c | Phone | Ref 3 Phone
Ref_4_Designation__c | Text | Ref 4 Designation
Ref_4_Email__c | Email | Ref 4 Email
Ref_4_Name__c | Text | Ref 4 Name
Ref_4_Notified__c | Checkbox | Ref 4 Notified
Ref_4_Phone__c | Phone | Ref 4 Phone
Reminder_Count__c | Number | Reminder Count
Request_Raised__c | Checkbox | Request Raised
Resistance_R__c | Number | Resistance R
Resume_Link__c | Url | Resume Link
Round__c | Picklist | Round | values: Initial; First; Second; Third; Fourth; Fifth; Sixth; Seventh; Eighth; Ninth; Tenth; Eleventh; Twelfth; Thirteenth; Fourteenth; Fifteenth; Final
Round_Info__c | Picklist | Round Info | values: Hiring Manager; Leet code style Coding; System Design; Coding Fronted; Backend Coding; AI/ML Coding; Offer Negotiation; Final Round; Other; Introduction Call; Technical Discussion; Executive Round; Behavioral Round; Coding Round; Healthcare Round; Preparation Round; Team Matching Round
Round_Info_if_Other__c | TextArea | Round Info if (Other)
Round_Multiplier__c | Formula<Number> | Round Multiplier
Round_Multiplier_New__c | Number | Round Multiplier New
Round_Number__c | Number | Round Number
Round_Rank__c | Number | Round Rank
Round_ranking_reason__c | LongTextArea | Round ranking reason
Salary_Compensation__c | Text | Salary Compensation
Schedule_Date__c | Date | Schedule Date
Scheduled_Minutes__c | Formula<Number> | Scheduled Minutes
Score_After_AIML__c | Number | Score After AIML
Score_After_Backend__c | Number | Score After Backend
Score_After_FinalRnd__c | Number | Score After FinalRnd
Score_After_Frontend__c | Number | Score After Frontend
Score_After_Leetcode__c | Number | Score After Leetcode
Score_After_Overall__c | Number | Score After Overall
Score_After_SysDesign__c | Number | Score After SysDesign
Score_Before_AIML__c | Number | Score Before AIML
Score_Before_Backend__c | Number | Score Before Backend
Score_Before_FinalRnd__c | Number | Score Before FinalRnd
Score_Before_Frontend__c | Number | Score Before Frontend
Score_Before_Leetcode__c | Number | Score Before Leetcode
Score_Before_Overall__c | Number | Score Before Overall
Score_Before_SysDesign__c | Number | Score Before SysDesign
Slack_Thread_Timestamp__c | Text | Slack Thread Timestamp
Start_Time__c | Time | Start Time(Do Not Use)
Start_Time_Text__c | Formula<Text> | Start Time Text
StartTime__c | Picklist | Start Time | globalValueSet: Times
Status_Change__c | Checkbox | Status Change
Status_New_Reason__c | Picklist | Status  Reason | values: Candidate requested change; Interviewer requested change; Company / Hiring Manager requested change; Scheduling conflict; Time zone confusion; Technical issue; Candidate not prepared; Panel unavailable; Emergency / personal reason; Candidate withdrew; Company cancelled role; Position filled; Duplicate interview; Candidate not eligible; Candidate no longer interested; Internal team unavailable; Interviewer unavailable; Technical / system issue; Candidate no show; Interviewer no show; Candidate joined late beyond cutoff; Interviewer joined late beyond cutoff; Wrong meeting link / access issue; Internet / technical issue; Time zone misunderstanding; Candidate unreachable; Interviewer unreachable; Emergency / personal issue; Other
Status_Reason__c | Picklist | Outcome Reason | values: Candidate Issue; Support Issue; Network Issue; AI Issue; Company Issue; Work Auth/Visa Mismatch; NA
Status_Reason_Comment__c | LongTextArea | Status Reason Comment
Submission_Rate__c | Currency | Submission Rate
Support_not_provided_reason__c | LongTextArea | Support not provided reason
Support_Notes__c | Html | Support Notes
Unique_Interview_Month_Key__c | Formula<Text> | Unique Interview Month Key
User_Initiated_By__c | Lookup(User) | User Initiated By
User_Initiated_On__c | DateTime | User Initiated On
varPreferred_support_person__c | Lookup(Recruiter__c) | Preferred support person TL
Velocity_Alignment__c | Number | Velocity Alignment
Velocity_EMA_AIML__c | Number | Velocity EMA AIML
Velocity_EMA_Backend__c | Number | Velocity EMA Backend
Velocity_EMA_FinalRnd__c | Number | Velocity EMA FinalRnd
Velocity_EMA_Frontend__c | Number | Velocity EMA Frontend
Velocity_EMA_Leetcode__c | Number | Velocity EMA Leetcode
Velocity_EMA_Overall__c | Number | Velocity EMA Overall
Velocity_EMA_SysDesign__c | Number | Velocity EMA SysDesign
Velocity_Factor_Fvel__c | Number | Velocity Factor Fvel
Vendor__c | Lookup(Vendor__c) | Proxy Support
Vendor_Feedback__c | LongTextArea | Vendor Feedback
Vendor_Name__c | Text | Vendor Name
View_Recording__c | Formula<Text> | View Recording
Volatility_Damp_Fv__c | Number | Volatility Damp Fv
Volatility_Sigma__c | Number | Volatility Sigma
Weighted_Credit__c | Formula<Number> | Weighted Credit
Weighted_Credit_New__c | Number | Weighted Credit
Why_not_eligible__c | LongTextArea | Why not eligible?
Zeta_Applied__c | Number | Zeta Applied
Zoom_Join_URL__c | Url | Zoom Join URL
Zoom_Meeting_Id__c | Text | Zoom Meeting Id
Zoom_Passcode__c | Text | Zoom Passcode
Zoom_Start_URL__c | Url | Zoom Start URL
```

## Interview_Evaluation__c - Interview Evaluation (6 fields)

```
Comment_Summary__c | LongTextArea | Comment Summary
Decision__c | Picklist | Decision | values: Hire; NoHire; Retake; Pass; Fail; Needs Improvement
Evaluation_Type__c | Picklist | Evaluation Type | values: Human; AI
Evaluator__c | Lookup(Employee) | Evaluator
Internal_Interview__c | MasterDetail(Internal_Interview__c) | Internal Interview
Total_Score__c | Number | Total Score
```

## Interview_Participant__c - Interview Participant (12 fields)

```
Attendance_Status__c | Picklist | Attendance Status | values: Invited; Joined; No Show; Cancelled; Left Early
Company__c | Text | Company
Email__c | Email | Email
Employee__c | Lookup(Recruiter__c) | Employee
Interview__c | MasterDetail(Interview__c) | Interview
Is_Primary__c | Checkbox | Is Primary
Joined_At__c | DateTime | Joined At
Left_At__c | DateTime | Left At
Participant_Name__c | Text | Participant Name
Participant_Type__c | Picklist | Participant Type | values: Candidate; Client Interviewer; Client Recruiter; Hiring Manager; HR; Internal Observer; Internal Support; Internal Trainer
Side__c | Picklist | Side | values: Candidate; Internal; Client
User__c | Lookup(User) | User
```

## Interview_Type__c - Interview Type (4 fields)

```
Is_Active__c | Checkbox | Is Active
Program_Version__c | Lookup(Program_Version__c) | Program Version
Requires_Hired_Niche__c | Checkbox | Requires Hired Niche
Uses_Retake_Decision__c | Checkbox | Uses Retake Decision
```

## Invoice__c - Invoice (64 fields)

```
ACH_Amount__c | Formula<Currency> | ACH Amount
Agreement_Percent__c | Percent | Agreement %
Amount_Paid__c | Summary | Amount Paid
Annual_Revenue__c | Formula<Currency> | Annual Revenue
Annual_Salary__c | Currency | Annual Salary
Auto_Send_Email__c | Checkbox | Auto Send Email
Bank_Account_Last4__c | Text | Bank Account (Last 4)
Bank_Setup_Summary__c | Text | Bank Setup Summary
Bonus_Amount__c | Currency | Bonus (if any)
Cancellation_Reason__c | LongTextArea | Cancellation Reason
Cancelled_Date__c | Date | Cancelled Date
Candidate__c | Lookup(Account) | Candidate
Charge_Interest__c | Checkbox | Charge Interest
Day_Of_Month__c | Number | Day of Month
Day_Of_Week__c | Picklist | Day of Week | values: Sunday; Monday; Tuesday; Wednesday; Thursday; Friday; Saturday
Days_In_Advance__c | Number | Days In Advance
Due_Date__c | Date | Due Date
Email_Sent_Date__c | DateTime | Email Sent Date
Frequency__c | Picklist | Frequency | values: Daily; Weekly; Monthly
Interest_Amount__c | Formula<Currency> | Interest Amount
Interest_Rate_Per_Cycle__c | Percent | Interest Rate / Cycle
Interval__c | Number | Interval
Invoice_Amount__c | Currency | Invoice Amount
Invoice_Date__c | Date | Invoice Date
Invoice_Id__c | Text | Invoice Id | externalId
Invoice_Link__c | Formula<Text> | Invoice Link
Invoice_Status__c | Picklist | Invoice Status | values: Not Paid; Partially Paid; Paid; Dispute; Void
Is_Recurring__c | Formula<Checkbox> | Is Recurring
Last_Recurring_Action__c | Text | Last Recurring Action
Last_Sync_Date__c | DateTime | Last Sync Date
Lead__c | Lookup(Lead) | Lead
Monthly_Revenue__c | Formula<Currency> | Monthly Revenue
Next_Billing_Date__c | Date | Next Billing Date
Number_of_Months__c | Number | Number of Cycles
Outstanding_Amount__c | Formula<Currency> | Outstanding Amount
Outstanding_Cycles__c | Formula<Number> | Outstanding Cycles
Payment_Link__c | Formula<Text> | Payment Link
Previous_Billing_Date__c | Date | Previous Billing Date
QB_Bank_Account_Id__c | Text | QB Bank Account Id
QB_Created_From__c | Picklist | QB Created From | values: Lead; Account; Manual
QB_Customer_Id__c | Formula<Text> | QB Customer Id
QB_Doc_Number__c | Text | QB Doc Number
QB_Email_Status__c | Picklist | QB Email Status | values: NotSet; EmailSent; NeedToSend
QB_Invoice_Id__c | Text | QB Invoice Id | externalId
QB_Recur_Data_Ref__c | Text | QB Recur Data Ref | externalId
QB_Recurring_ID__c | Text | QB Recurring ID | unique | externalId
Recurring_Amount__c | Currency | Recurring Amount
Recurring_Deposit_Account__c | Text | Recurring Deposit Account
Recurring_Description__c | LongTextArea | Recurring Description
Recurring_End_Date__c | Date | Recurring End Date
Recurring_Payment_Method__c | Picklist | Recurring Payment Method | values: Credit Card; ACH/Bank Transfer; Check; Cash; Other
Recurring_Plan__c | Text | Recurring Plan
Recurring_Start_Date__c | Date | Recurring Start Date
Recurring_Status__c | Picklist | Recurring Status | values: Active; Awaiting Payment Method; Payment Method Not Added; Paused; Cancelled; Completed; Failed
Recurring_Template_Name__c | Text | Recurring Template Name
Schedule_Description__c | Formula<Text> | Schedule Description
Slack_Owner_Thread_Ts__c | Text | Slack Owner Thread Ts
Slack_Thread_Ts__c | Text | Slack Thread Ts
Total_Amount_Collected__c | Summary | Total Amount Collected
Total_Cycles_Completed__c | Summary | Total Cycles Completed
Total_Plan_Value__c | Formula<Currency> | Total Plan Value
Total_Salary__c | Formula<Currency> | Total Salary
Type__c | Picklist | Type | values: One-Time; Recurring
Week_Of_Month__c | Picklist | Week of Month | values: First; Second; Third; Fourth; Last
```

## Job_Requirement__c - Job Requirement (38 fields)

```
Account_Manager__c | Lookup(User) | Account Manager
Client_Account__c | Lookup(Account) | Client Account
Client_Bill_Rate__c | Currency | Client Bill Rate
Client_Company__c | Lookup(Company__c) | Client Company
Client_Contact__c | Lookup(Contact) | Client Contact
Client_Lead__c | Lookup(Lead) | Client Lead
Client_Submission_Count__c | Number | Client Submission Count
Duration__c | Text | Duration
Employment_Type__c | Picklist | Employment Type | values: C2C; W2 Contract; Full Time; Contract-to-Hire
Estimated_Margin__c | Formula<Currency> | Estimated Margin
External_Job_ID__c | Text | External Job ID | unique | externalId
Has_Client_Side__c | Formula<Checkbox> | Has Client Side
Has_Prospect_Side__c | Formula<Checkbox> | Has Prospect Side
Interview_Count__c | Number | Interview Count
Job_Description__c | Html | Job Description
Job_Status__c | Picklist | Job Status | values: New; Open; On Hold; Closed
Job_Title__c | Text | Job Title
Last_Notification_Sent__c | DateTime | Last Notification Sent
Location__c | Text | Location
Max_Submissions_Allowed__c | Number | Max Submissions Allowed
Minimum_Experience_Required__c | Text | Experience Required
Number_of_Openings__c | Number | Number of Openings
Other_Closure_Reason__c | Text | Other Closure Reason
Primary_Skills__c | LongTextArea | Primary Skills
Priority__c | Picklist | Priority | values: Urgent; High; Medium
Public_Website_Description__c | LongTextArea | Public Website Description
Publish_to_Website__c | Checkbox | Job Post on Website
Rate_Type__c | Picklist | Rate Type | values: Hourly; Salary; Daily
Reason_for_JR_Closure__c | Picklist | Reason for JR Closure | values: Completed; Cancelled; No Response; Expired; Other
Required_Visa_Status__c | MultiselectPicklist | Required Visa Status | values: USC; GC; H1B; H4 EAD; OPT; CPT; TN; Any
Requirement_Received_Date_Time__c | DateTime | Requirement Received Date/Time
Show_Client_Name_on_Website__c | Checkbox | Show Client Name on Website
SLA_Status__c | Formula<Text> | SLA Status
Submission_Count__c | Number | Submission Count
Submission_Deadline__c | DateTime | Submission Deadline | required
Target_Candidate_Rate__c | Currency | Target Candidate Rate
Website_Status__c | Picklist | Job Posting Status | values: Draft; Published; Unpublished
Work_Mode__c | Picklist | Work Mode | values: Remote; Hybrid; Onsite
```

## Job_Submission__c - Job Submission (28 fields)

```
Account_Manager__c | Lookup(User) | Account Manager
Applicant_Email__c | Email | Applicant Email
Applicant_Name__c | Text | Applicant Name
Availability__c | Text | Availability
Candidate__c | Lookup(Account) | Candidate
Candidate_Rate__c | Currency | Candidate Rate
Candidate_Summary__c | LongTextArea | Candidate Summary
Client_Account__c | Lookup(Account) | Client Account
Client_Feedback__c | LongTextArea | Client Feedback
Client_Lead__c | Lookup(Lead) | Client Lead
Client_Submission_Date_Time__c | DateTime | Client Submission Date/Time
Expected_Margin__c | Formula<Currency> | Expected Margin
Internal_Submission_Date_Time__c | DateTime | Internal Submission Date/Time
Interview_Requested__c | Checkbox | Interview Requested
Job_Requirement__c | Lookup(Job_Requirement__c) | Job Requirement
Lead__c | Lookup(Lead) | Lead
Offer_Received__c | Checkbox | Offer Received
Placement_Status__c | Picklist | Placement Status | values: Not Placed; Offered; Placed; Lost
Rate_Type__c | Picklist | Rate Type | values: Hourly; Salary; Daily
Rejection_Reason__c | Picklist | Rejection Reason | values: Rate High; Skill Mismatch; Visa; Location; Availability; No Response; Duplicate; Other
Skills_Match_Notes__c | LongTextArea | Skills Match Notes
Slack_Thread_ID__c | Text | Slack Thread ID
Submission_Source__c | Picklist | Submission Source | values: Internal Recruiter; Website Applicant; Candidate Portal
Submission_Status__c | Picklist | Submission Status | values: Pending AM Review; Approved; Submitted to Client; Client Shortlisted; Interview Scheduled; Interview Completed; Shortlisted for Next Round; Rejected; Offer Received; Placed; Withdrawn
Submitted_Bill_Rate__c | Currency | Submitted Bill Rate
Submitted_By__c | Lookup(User) | Submitted By
Unique_Submission_Key__c | Text | Unique Submission Key | unique
Visa_Confirmation__c | Text | Visa Confirmation
```

## Marketing__c - Marketing (18 fields)

```
Actual_Joining_Date__c | Date | Actual Joining Date
Assign_to_Lead__c | Checkbox | Assign to Lead
Candidate__c | Lookup(Account) | Candidate | required
Effective_Recruiter_Name__c | Formula<Text> | Effective Recruiter Name
Initial_call_completed__c | Checkbox | Initial call completed
Introduction_script_created__c | Checkbox | Introduction script created
Job_boards_created__c | Checkbox | Job boards created
Marketing_Type__c | Picklist | Marketing Type | values: Full time; C2C
No_of_Interviews_rescheduled_by_candidat__c | Number | No of Interviews rescheduled by candidat
Offer_Letter_Date__c | Date | Offer Letter Date
Reason_for_paused__c | LongTextArea | Reason for pause
Reason_for_Stop__c | LongTextArea | Reason for Stop
Recruiter__c | Lookup(Recruiter__c) | Recruiter
Resume_Understanding_Session__c | Checkbox | Resume Understanding Session
Senior_Recruiter_Name__c | Formula<Text> | Senior Recruiter Name
Status__c | Picklist | Status | values: New Candidate; Initial Call; Applications; Offer Letter; Closed; Paused; Stopped
Tentative_Joining_Date__c | Date | Tentative Joining Date
TL_Recruiter_Name__c | Formula<Text> | TL Recruiter Name
```

## Niche__c - Niche (2 fields)

```
Domain_Type__c | Picklist | Domain Type | values: Tech; Non Tech
Is_Active__c | Checkbox | Is Active
```

## Niche_Question__c - NicheQuestion (2 fields)

```
Niche__c | MasterDetail(Niche__c) | Niche
Question__c | MasterDetail(Question_Bank__c) | Question
```

## Onboarding__c - Onboarding (153 fields)

```
Account__c | Lookup(Account) | Candidate Name
Account_information_collected__c | Checkbox | Account information collected
Active_CPT__c | Checkbox | Active CPT?
Assigned_to_Marketing__c | Checkbox | Assigned to Marketing
ATS_Score__c | Number | ATS Score
Authorized_To_Work_In_US__c | Checkbox | Authorized To Work In US?
Billing_Address__c | Text | Billing Address
Candidate_Approval__c | Checkbox | Candidate Approval
Candidate_Email__c | Email | Candidate Email
Candidate_Form_Additional_Details__c | LongTextArea | Candidate Form Additional Details
Candidate_form_filled__c | Checkbox | Candidate form filled
Candidate_form_sent__c | Checkbox | Candidate form sent
Candidate_Time_Zone__c | Picklist | Candidate Time Zone | globalValueSet: Candidate_Time_Zone
Certifications__c | LongTextArea | Certifications
CGPA_Score__c | Number | CGPA Score
College_Name__c | Text | College Name
College_Name_2__c | Text | College Name 2
College_Name_3__c | Text | College Name 3
College_Name_4__c | Text | College Name 4
College_Name_5__c | Text | College Name 5
Company_name_1__c | Text | Company name 1
company_name_2__c | Text | company name 2
company_name_3__c | Text | company name 3
Company_Name_4__c | Text | Company Name 4
Company_Name_5__c | Text | Company Name 5
Course_Name__c | Lookup(Course__c) | Course Name
Current_Address__c | Address | Current Address
Current_Domain__c | Text | Current Domain
Daily_Interview_Availability__c | Text | Daily Interview Availability
Date_of_Arrival__c | Date | Date of Arrival
Date_of_Birth__c | Date | Date of Birth
Declaration__c | Checkbox | I confirm that all the above information
Degree__c | Picklist | Degree | globalValueSet: Degree
Degree_2__c | Picklist | Degree 2 | globalValueSet: Degree
Degree_3__c | Picklist | Degree 3 | globalValueSet: Degree
Degree_4__c | Picklist | Degree 4 | globalValueSet: Degree
Degree_5__c | Picklist | Degree 5 | globalValueSet: Degree
Degree_End_Date__c | Date | Degree End Date
Degree_End_Date_2__c | Date | Degree End Date 2
Degree_End_Date_3__c | Date | Degree End Date 3
Degree_End_Date_4__c | Date | Degree End Date 4
Degree_End_Date_5__c | Date | Degree End Date 5
Degree_Start_Date__c | Date | Degree Start Date
Degree_Start_Date_2__c | Date | Degree Start Date 2
Degree_Start_Date_3__c | Date | Degree Start Date 3
Degree_Start_Date_4__c | Date | Degree Start Date 4
Degree_Start_Date_5__c | Date | Degree Start Date 5
Degree_Type__c | Picklist | Degree Type | globalValueSet: Degree_Type
Description__c | LongTextArea | Description
Disability_Status__c | Picklist | Disability Status | values: Yes, I have a disability or previously had a disability; No, I do not have a disability; Prefer not to say
Duration_1__c | Text | Duration 1
Duration_2__c | Text | Duration 2
Duration_3__c | Text | Duration 3
Duration_4__c | Text | Duration 4
Duration_5__c | Text | Duration 5
Education_Country__c | Picklist | Education Country | globalValueSet: Country
Education_Details__c | LongTextArea | Education Details
End_Date__c | Date | End Date
End_Date_2__c | Date | End Date 2
End_Date_3__c | Date | End Date 3
End_Date_4__c | Date | End Date 4
End_Date_5__c | Date | End Date 5
Flow_Only_Field__c | Checkbox | Flow Only Field
Gender__c | Picklist | Gender | values: Male; Female; I_choose_not_to_disclose
Inactive_Reason__c | Text | Inactive Reason
Industry__c | Picklist | Industry | globalValueSet: Industry
Institute__c | Lookup(Education_History__c) | Institute
Institute_Country__c | Lookup(Education_History__c) | Institute Country
Institutes__c | Lookup(Institute__c) | Institute
Internal_Approval__c | Checkbox | Internal Approval
Job_Location__c | Text | Job Location
Job_Location_2__c | Text | Job Location 2
Job_Location_3__c | Text | Job Location 3
Job_Location_4__c | Text | Job Location 4
Job_Type__c | Picklist | Job Type | values: Remote; Hybrid; On-site; All of the above
Joining_Date__c | Date | Joining Date
Joining_Date_2__c | Date | Joining Date 2
Joining_Date_3__c | Date | Joining Date 3
Joining_Date_4__c | Date | Joining Date 4
Joining_Date_5__c | Date | Joining Date 5
Last_4_digits_of_SSN__c | Number | Last 4 digits of SSN
Latest_Onboarding_Form_Link__c | Url | Latest Onboarding Form Link
Latest_Onboarding_On__c | DateTime | Latest Onboarding  On
Latest_Onboarding_Opened_On__c | DateTime | Latest Onboarding Opened On
Latest_Onboarding_Sent_On__c | DateTime | Latest Onboarding Sent On
Latest_Onboarding_Status__c | Text | Latest Onboarding Status
LinkedIn_Email_Id__c | Email | LinkedIn Email Id
LinkedIn_Optimization__c | Checkbox | LinkedIn Optimization
LinkedIn_Password__c | EncryptedText | LinkedIn Password
LinkedIn_Password_Updated__c | Text | LinkedIn Password Updated
LinkedIn_Profile_URL__c | Url | LinkedIn Profile URL
Linkedin_URL__c | Url | Linkedin URL
LinkedIn_Username__c | Text | LinkedIn Username
List_of_Certifications__c | LongTextArea | List of Certifications
Location_1__c | Text | Location 1
Location_2__c | Text | Location 2
Location_3__c | Text | Location 3
Location_4__c | Text | Location 4
Location_5__c | Text | Location 5
Marketing_Email_Id__c | Text | Marketing Email Id
Marketing_Email_Password__c | Text | Marketing Email Password
Mention_your_Visa_dates_if_applicable__c | Text | Mention your Visa dates if applicable
Military_Status__c | Picklist | Military Status | values: Active Duty; Reserve or National Guard; Veteran; Retired Military; Military Spouse; No Military Service; Prefer not to say
Niche__c | Picklist | Niche | globalValueSet: Niche
Niche_Other__c | Text | Niche Other
No_of_Degrees__c | Number | No of Degrees
Notes__c | LongTextArea | Notes
Offer_letter_amount__c | Number | Offer letter amount
Onboarding_Form_Link__c | Url | Onboarding Form Link
Onboarding_Form_Status__c | Picklist | Onboarding Form Status | values: Sent; Opened; Draft; Submitted; Expired; Cancelled
Open_to_relocate__c | Checkbox | Open to relocate
Other_please_specify__c | Text | Other (please specify)
Owner_Name_Formula__c | Formula<Text> | Owner Name Formula
Passport_Number__c | Text | Passport Number
Phone__c | Phone | Phone
Plan__c | Picklist | Plan | values: Career Launcher; Career Accelerator; Premium / Fastrack; Ultimate Career Architect
Position_role_1__c | Text | Position role 1
Position_role_2__c | Text | Position role 2
Position_role_3__c | Text | Position role 3
Position_role_4__c | Text | Position role 4
Position_role_5__c | Text | Position role 5
Preferred_Job_Locations__c | Text | Preferred Job Locations
Preferred_Job_Positions__c | Text | Preferred Job Positions
Preferred_Tech_stack_If_Applicable__c | Text | Preferred Tech stack (If Applicable)
Professional_Profiles__c | LongTextArea | Professional Profiles
Race_Ethnicity__c | Picklist | Race/Ethnicity | values: American Indian or Alaska Native; Asian; Black or African American; Hispanic or Latino; Native Hawaiian or Other Pacific Islander; Two or More Races; Other (please specify); I choose not to disclose
Resume_Created__c | Checkbox | Resume Created
Resume_Creation_Notes__c | TextArea | Resume Creation Notes
Salary__c | Percent | Salary
Salary_Expectations__c | Text | Salary Expectations (Old Field)
Salary_Expectations_Picklist__c | Picklist | Salary Expectations | values: $60,000 - $80,000; $80,000 - $120,000; $120,000 - $160,000; $160,000 - $200,000; $200,000 +
Secondary_Email__c | Email | Secondary Email
Secondary_Phone__c | Phone | Secondary Phone
Service_Agreement_Sent__c | Checkbox | Service Agreement Sent
Service_Agreement_Signed__c | Checkbox | Service Agreement Signed
Shipping_Address__c | Text | Shipping Address
Sponsorship_Required_For_VISA__c | Checkbox | Sponsorship Required For VISA?
Start_Time__c | Picklist | Start Time | globalValueSet: Times
Status__c | Picklist | Status | values: Welcome Call; Service Agreement; Resume Creation; Onboarding Completed; Inactive
Technology__c | Text | Technology
Technology_For_Resume__c | LongTextArea | Technology For Resume
Total_Years_of_Experience__c | Number | Total Years of Experience
Types_of_degree__c | Text | Types of degree
Upfront_Amount__c | Number | Upfront Amount
Visa_Status__c | Picklist | Visa Status | values: F1; H1-B; CPT; OPT-EAD; H4-EAD; Green_Card; US_Citizen; Other
Welcome_Script__c | TextArea | Welcome Script
WhatsApp_Number__c | Phone | WhatsApp Number
When_did_you_move_to_USA__c | Text | When did you move to USA
Which_Experience_Level__c | Picklist | Which Experience Level | values: Entry_Level; Associate; Mid-Senior_level; Director; Executive
Work_Authorization__c | Picklist | Work Authorization | globalValueSet: Work_Authorization_Picklist
Work_Authorization_Expiry_Date__c | Date | Work Authorization Expiry Date
Work_Experiene__c | LongTextArea | Work Experience
Years_of_Experience__c | Number | Years of Experience
```

## Onboarding_Token__c - Onboarding Token (8 fields)

```
Expires_At__c | DateTime | Expires At
Onboarding__c | Lookup(Onboarding__c) | Onboarding
Onboarding_Form_Link__c | Text | Onboarding Form Link
Onboarding_Form_Status__c | Picklist | Onboarding Form Status | values: Sent; Opened; Submitted; Expired; Cancelled; Draft
Recipient_Email__c | Email | Recipient Email
Token__c | Text | Token | required | unique | externalId
Used__c | Checkbox | Used
Used_At__c | DateTime | Used At
```

## Payment__c - Payment (38 fields)

```
Amount_Paid__c | Currency | Amount Paid
Candidate_Name__c | Formula<Text> | Candidate Name
Card_Type__c | Picklist | Card Type | values: Visa; Mastercard; Amex; Discover; ACH; Other
CS_Notes__c | LongTextArea | CS Notes
CS_Person__c | Lookup(User) | CS Person
Deposit_Account_ID__c | Text | Deposit Account ID
Deposit_Amount__c | Currency | Deposit Amount
Deposit_Date__c | Date | Deposit Date
Deposit_Status__c | Picklist | Deposit Status | values: Pending; Deposited; Reversed
Deposit_To_Account__c | Text | Deposit To Account
Finance_Notes__c | LongTextArea | Finance Notes
Finance_Person__c | Lookup(User) | Finance Person
Invoice__c | MasterDetail(Invoice__c) | Invoice
Last_Deposit_Check_Date__c | DateTime | Last Deposit Check
Name_Hyperlink__c | Formula<Text> | Name Hyperlink
Payment_Amount__c | Currency | Payment Amount
Payment_Date__c | Date | Payment Date
Payment_Issue_Amount__c | Currency | Issue Amount
Payment_Issue_Case_Number__c | Text | Case / Reference #
Payment_Issue_Outcome__c | Picklist | Outcome | values: Won; Lost; Refunded; Re-collected; Written Off
Payment_Issue_Reason__c | Text | Issue Reason
Payment_Issue_Received_Date__c | Date | Received Date
Payment_Issue_Resolved_Date__c | Date | Resolved Date
Payment_Issue_Respond_By__c | Date | Respond By (Chargebacks Only)
Payment_Issue_Status__c | Picklist | Issue Status | values: Open; In Progress; Pending Finance; Submitted to Bank; Resolved
Payment_Issue_Type__c | Picklist | Payment Return Type | values: Dispute; ACH Return; Bank Transfer Canceled; Retrieval Request
Payment_Source__c | Picklist | Payment Source | values: Invoice Payment; Sales Receipt; Manual
Payment_Status__c | Picklist | Payment Status | values: Paid; Voided
Payment_Type__c | Picklist | Payment Type | values: Full; Partial
QB_Deposit_ID__c | Text | QB Deposit ID | externalId
QB_Doc_Number__c | Text | QB Doc Number
QB_Operation__c | Text | QB Operation
QB_Payment_Id__c | Text | QB Payment Id | externalId
QB_Sales_Receipt_ID__c | Text | QB Sales Receipt ID | unique | externalId
Refund_Receipt_Created__c | Checkbox | Refund Receipt Created
Scheduled_Date__c | Date | Scheduled Date
Transaction_Id__c | Text | Transaction Id
Transaction_Status__c | Picklist | Transaction Status | values: Scheduled; Pending; Successful; Failed; Cancelled; Disputed
```

## Pre_Enrolment_Request__c - Pre Enrolment Request (29 fields)

```
Acknowledgement_Declaration__c | Checkbox | Acknowledgement Declaration
Additional_Comments_or_Questions__c | LongTextArea | Additional Comments or Questions
Candidate_Email_Snapshot__c | Email | Candidate Email Snapshot
Candidate_Name_Snapshot__c | Text | Candidate Name Snapshot
Contact_Number_Whatsapp__c | Phone | Contact Number Whatsapp
Criminal_Record_Declaration__c | Picklist | Criminal Record Declaration | values: I do NOT have any criminal record; I have a criminal record (details will be discussed privately)
Current_State_in_USA__c | Picklist | Current State in USA | globalValueSet: State
Current_Visa_Status__c | Picklist | Current Visa Status | globalValueSet: Visa_Status
Email_Address__c | Email | Email Address
Expiration_Datetime__c | DateTime | Expiration Datetime
First_Name__c | Text | First Name
Home_Address__c | Address | Home Address
Last_Name__c | Text | Last Name
Lead__c | Lookup(Lead) | Lead
Middle_Name__c | Text | Middle Name
Niche__c | Picklist | Niche | globalValueSet: Niche
Niche_Other__c | TextArea | Niche Other
Opened_On__c | DateTime | Opened On
Position_Type_C2C__c | Checkbox | Position Type C2C
Position_Type_Full_Time_CTH__c | Checkbox | Position Type Full Time CTH
Preferred_Training_Schedule_EST__c | Picklist | Preferred Training Schedule EST | globalValueSet: Training_Schedule
Public_Form_Link__c | Url | Public Form Link
Public_Token__c | TextArea | Public Token
Relocation_Other__c | TextArea | Relocation Other
Relocation_Readiness__c | Picklist | Relocation Readiness | values: Yes, I am willing to relocate anywhere in the USA; Yes, but only within specific states (mention in comments); No, I prefer remote / local positions only; Other
screen_calling__c | Phone | Contact Number Calling
Sent_On__c | DateTime | Sent On
Status__c | Picklist | Status | values: Draft; Sent; Opened; Submitted; Expired; Cancelled
Submitted_On__c | DateTime | Submitted On
```

## Program__c - Program (4 fields)

```
Auto_Create_Sessions__c | Checkbox | Auto-Create Sessions
Description__c | LongTextArea | Description
Lifecycle_Status__c | Picklist | Lifecycle Status | required | values: Draft; Published; Archived
Session_Type__c | Picklist | Session Type | values: Single Session; Group Session
```

## Program_Version__c - Program Version (28 fields)

```
Booking_Mode__c | Picklist | Booking Mode | values: Anytime; Fixed Schedule
Booking_Owner__c | Picklist | Booking Owner | values: Trainer Books; Candidate Books
Duration_Days__c | Number | Duration (Days)
Effective_From__c | DateTime | Effective From
Has_Mock__c | Checkbox | Include Mock Interview
Mock_Blocks_Progress__c | Checkbox | Mock Blocks Progress
Mock_Booking_Owner__c | Picklist | Mock Booking Owner | values: Trainer Books; Candidate Books
Mock_Booking_Window_Days__c | Number | Mock Booking Window (working days)
Mock_Cadence_Day__c | Picklist | Mock Cadence Day | values: Monday; Tuesday; Wednesday; Thursday; Friday
Mock_Duration_Min__c | Number | Mock Duration (min)
Mock_Failure_Requires_Retake__c | Checkbox | Mock Failure Requires Retake
Mock_Final_Week_Day__c | Picklist | Mock Final Week Day | values: Monday; Tuesday; Wednesday; Thursday; Friday
Mock_Frequency__c | Picklist | Mock Frequency | values: Weekly; Once; End of Program; Custom (After Specific Steps)
Mock_Interviewer_Group__c | Text | Mock Interviewer Public Group
Mock_Miss_Action__c | Picklist | Mock Miss Action | values: Drop Training; Warn Only; Notify Admin
Mock_Overflow_Threshold__c | Number | Mock Overflow Threshold
Mock_Reschedule_Lock_Days__c | Number | Mock Reschedule Lock (working days)
Mock_Result_Deadline_Days__c | Number | Mock Result Deadline (working days)
Mock_Slot_Granularity_Min__c | Number | Mock Slot Granularity (min)
Notes__c | LongTextArea | Notes
Program__c | Lookup(Program__c) | Program | required
Session_Duration__c | Picklist | Session Duration | values: 15; 30; 45; 60; 75; 90; 105; 120; 135; 150; 165; 180
Session_Duration_Min__c | Number | Session Duration (Min) [Deprecated]
Session_Type__c | Picklist | Session Type | values: Single Session; Group Session
Sessions_Per_Day__c | Number | Sessions Per Day
Status__c | Picklist | Status | required | values: Draft; Published; Superseded
Total_Expected_Duration__c | Summary | Total Expected Duration
Version_Number__c | Number | Version Number
```

## QB_Plan__c - QB Plan (1 fields)

```
Item_Number__c | Text | Item Number
```

## Question_Bank__c - Question Bank (11 fields)

```
Difficulty_Level__c | Picklist | Difficulty Level | values: Easy; Medium; Hard
Evaluation_Criteria__c | LongTextArea | Evaluation Criteria
Interview_Type__c | Lookup(Interview_Type__c) | Interview Type (DEPRECATED - do not use)
Is_Active__c | Checkbox | Is Active
Niche__c | Lookup(Niche__c) | Niche (DEPRECATED - do not use)
Notebook_Link__c | Url | Notebook Link
Question_Hash__c | Text | Question Hash | unique | externalId
Question_Text__c | LongTextArea | Question Text
Scenario__c | LongTextArea | Scenario
Section__c | Lookup(Section__c) | Section
Section_Name__c | Formula<Text> | Section Name
```

## Question_Follow_Up__c - Question Follow Up (4 fields)

```
Follow_Up_Answer__c | LongTextArea | Follow Up Answer
Follow_Up_Question__c | LongTextArea | Follow Up Question
Question__c | MasterDetail(Question_Bank__c) | Question
Sequence__c | Number | Sequence
```

## Question_Interview_Type__c - Question Interview Type (2 fields)

```
Interview_Type__c | MasterDetail(Interview_Type__c) | Interview Type
Question__c | MasterDetail(Question_Bank__c) | Question
```

## Question_Section__c - Question Section (2 fields)

```
Question__c | MasterDetail(Question_Bank__c) | Question
Section__c | MasterDetail(Section__c) | Section
```

## Recruiter__c - Employees (47 fields)

```
Active_For_Interview_Support__c | Checkbox | Active For Interview Support
Active_For_Mock_Interview__c | Checkbox | Active For Mock Interview
Allow_Buffer_Time__c | Checkbox | Allow Buffer Time
Assigned_Sr_Recruiter__c | Lookup(Recruiter__c) | Reporting to
Buffer_Minutes_Before_After__c | Number | Buffer Minutes Before/After
COMPASS_State_JSON__c | LongTextArea | COMPASS State JSON
COMPASS_State_Last_Saved_UTC__c | DateTime | COMPASS State Last Saved (UTC)
COMPASS_State_Schema_Version__c | Text | COMPASS State Schema Version
Confidence_score__c | TextArea | Confidence score
Confidence_Score_AI_ML_Coding__c | Number | Confidence_Score_AI/ML_Coding
Confidence_Score_Backend_Coding__c | Number | Confidence_Score_Backend_Coding
Confidence_Score_Final_Round__c | Number | Confidence_Score_Final_Round
Confidence_Score_Frontend_Coding__c | Number | Confidence_Score_Frontend_Coding
Confidence_Score_Leetcode__c | Number | Confidence_Score_Leetcode
Confidence_Score_System_Design__c | Number | Confidence_Score_System_Design
Daily_Max_Interview_Hours__c | Number | Daily Max Interview Hours
Date_of_Birth__c | Date | Date of Birth
Date_of_Joining__c | Date | Date of Joining
Department__c | Lookup(Department__c) | Department
Description__c | LongTextArea | Description
Email__c | Email | Email
Employment_Status__c | Picklist | Employment Status | values: Active Employee; Former Employee; Leave of Absense
End_Time_IST__c | Picklist | End Time IST | globalValueSet: Times
Experience_Level__c | Picklist | Experience Level | values: Fresher level; Beginner Level; Mid Level; Senior Level
First_Name__c | Text | First Name
Last_Name__c | Text | Last Name
Last_Working_Date__c | Date | Last Working Date
Leave_End_Date_Time__c | DateTime | Leave End Date/Time
Leave_Start_Date_Time__c | DateTime | Leave  Start Date/Time
No_of_Assigned_Candidates__c | Number | No of Assigned Candidates
On_break__c | Checkbox | On break
On_Leave_Today__c | Picklist | On Leave Today | values: Half Day; Full Day
PersonAccount__c | Text | PersonAccount
Phone_No__c | Text | Phone No
Preferred_Lunch_Start_IST__c | Time | Preferred Lunch Start (IST)
Primary_Track__c | Picklist | Primary Track | values: Salesforce; Data; QA; BA
Recruiter_Contact__c | Lookup(Account) | Employee Contact
Recruiter_User__c | Lookup(User) | Recruiter User
Secondary_Track__c | Picklist | Secondary Track | values: Salesforce; Data; QA; BA
Seniority_Level__c | Picklist | Seniority Level | values: Junior; Mid; Senior; Lead
Slack_User_Id__c | Text | Slack User Id
Start_Time_IST__c | Picklist | Start Time IST | globalValueSet: Times
Target_Per_Candidate__c | Number | Target Per Candidate
Tomorrow_Availability__c | Checkbox | Tomorrow Availability
Total_Target__c | Number | Total Target
Workday_End_Time_IST__c | Time | Workday End Time (IST)
Workday_Start_Time_IST__c | Time | Workday Start Time (IST)
```

## Recurring_Break_Series__c - Recurring Break Series (8 fields)

```
Description__c | Text | Description
End_Date__c | Date | End Date
End_Time__c | Text | End Time
Event_Count__c | Number | Event Count
Start_Date__c | Date | Start Date
Start_Time__c | Text | Start Time
Subject__c | Text | Subject
Time_Zone__c | Picklist | Time Zone | globalValueSet: Candidate_Time_Zone
```

## Resume__c - Resume Creation (10 fields)

```
Approval_Date__c | Date | Approval Date
Approved_By__c | Lookup(Recruiter__c) | Approved  By
Candidate__c | Lookup(Account) | Candidate
Internal_Interview__c | Lookup(Internal_Interview__c) | Internal Interview
Is_Active__c | Checkbox | Is Active
Lead__c | Lookup(Lead) | Lead
Niche__c | Lookup(Niche__c) | Niche
Notes__c | LongTextArea | Notes
Resume_Status__c | Picklist | Resume Status | values: Draft; Under Review; Approved; Rejected; Uploaded
Uploaded_By__c | Lookup(Recruiter__c) | Uploaded By
```

## Section__c - Section (11 fields)

```
Domain_Type__c | Picklist | Domain Type | values: Tech; Non-Tech; Both
ESTIMATED_TIME_IN_MINUTES__c | Text | Estimated Time(In Minutes)
Exclude_Source_Section__c | Lookup(Section__c) | Exclude Source Section
Interview_Sequence__c | Number | Interview Sequence
Interview_Type__c | Lookup(Interview_Type__c) | Interview Type (DEPRECATED - do not use)
Is_Active__c | Checkbox | Is Active
Niche__c | Lookup(Niche__c) | Niche (DEPRECATED - do not use)
Question_Source_Type__c | Picklist | Question Source Type | values: Standard; AI Resume Based
Source_Interview_Type__c | Lookup(Interview_Type__c) | Source Interview Type
Source_Section__c | Lookup(Section__c) | Source Section (only)
Total_Questions__c | Number | Total Questions
```

## Section_Evaluation__c - Section Evaluation (4 fields)

```
Comment__c | LongTextArea | Comment
Evaluation__c | MasterDetail(Interview_Evaluation__c) | Evaluation
Score__c | Number | Score
Section__c | Lookup(Section__c) | Section
```

## Section_Interview_Type__c - Section Interview Type (3 fields)

```
Interview_Type__c | MasterDetail(Interview_Type__c) | Interview Type
Section__c | MasterDetail(Section__c) | Section
Sequence__c | Number | Sequence
```

## Session__c - Session (71 fields)

```
Actual_End__c | DateTime | Actual End
Actual_Start__c | DateTime | Actual Start
Additional_Attendee_1__c | Lookup(Recruiter__c) | Additional Attendee 1
Additional_Attendee_2__c | Lookup(Recruiter__c) | Additional Attendee 2
Analysis_Status__c | Picklist | Analysis status | values: Pending; Complete; Failed
Analyzed_At__c | DateTime | Analyzed at
Approx_time__c | Text | Approx  time
Attendance_status__c | MultiselectPicklist | Attendance status | values: Host Skipped; Attendee Skipped; Attendee 1 Skipped; Attendee 2 Skippped
Background_Check__c | Lookup(Background_Check__c) | Background Check
calendar_event_id__c | Text | calendar event id
calendar_link__c | Url | calendar link
Candidate__c | Lookup(Account) | Candidate
Candidate_Email__c | Formula<Text> | Candidate Email
Candidate_Identities__c | Number | Candidate identities
Candidate_Name__c | Formula<Text> | Candidate Name
Candidate_On_Camera_Pct__c | Percent | Candidate on-camera %
Candidate_Talk_Pct__c | Percent | Candidate talk %
Candidate_Training__c | Lookup(Candidate_Training__c) | Candidate Training
Candidate_Training_Step__c | Lookup(CandidateTrainingStep__c) | Candidate Training Step
Cohort__c | Lookup(Cohort__c) | Cohort
Coverage_Detail__c | Text | Coverage detail
Create_Meeting_Error__c | TextArea | Create Meeting Error
Day_Title__c | Text | Day title
Description__c | LongTextArea | Description
Duration_Minutes__c | Number | Duration (minutes)
End_Time_IST__c | Text | End Time IST
Evidence__c | Text | Evidence
External_Meeting_ID__c | Text | External Meeting ID
External_Meeting_Passcode__c | Text | External Meeting Passcode
Flag_Type__c | Picklist | Flag Type | values: person change midsession; fabricated experience; scripted deception; proxy interview coaching; candidate camera off; gaze fixed offscreen; session too short; key section rushed; non english heavy
Host_Feedback__c | LongTextArea | Host Feedback
Host_User__c | Lookup(Recruiter__c) | Host User
Integrity_Score__c | Number | Session integrity score
Integrity_Tier__c | Picklist | Integrity tier | values: Clean; Review; High-risk
Internal_Interview__c | Lookup(Internal_Interview__c) | Internal Interview
Interview__c | Lookup(Interview__c) | Interview
Lead__c | Lookup(Lead) | Lead
Max_Reading_Likelihood_Pct__c | Percent | Max reading likelihood %
Meeting_Link__c | Url | Meeting Link
MeetingTopic__c | Formula<Text> | MeetingTopic
Non_English_Seconds__c | Number | Non-English seconds
Onboarding__c | Lookup(Onboarding__c) | Onboarding
Planned_Minutes__c | Number | Planned minutes
Points__c | Number | Points
Program_Version__c | Lookup(Program_Version__c) | Program Version
Proof_path__c | Text | Proof path
Purpose__c | Picklist | Purpose | values: Training; Internal Interview; Welcome Call â€” Customer Success; Resume Understanding Session; Initial Call â€” Marketing; Background Check
Recording_URL__c | Url | Recording URL
Red_Flags_Summary__c | LongTextArea | Red flags summary
Report_URL__c | Url | Report URL
Result_JSON_URL__c | Url | Result JSON URL
S3_Prefix__c | Text | S3 prefix
Same_Person_Throughout__c | Checkbox | Same person throughout
Scheduled_Date__c | Date | Date Of Session
Scheduled_End__c | DateTime | Selected Time Zone End Time
Scheduled_Start__c | DateTime | Selected Time Zone  Start  Time
Selected_Time_Zone_ETim__c | Picklist | Selected Time Zone End Time | globalValueSet: Times
Selected_Time_Zone_Start_Time__c | Picklist | Selected Time Zone Start Time | globalValueSet: Times
Session_Length_Pct__c | Percent | Session length %
Session_Summary__c | LongTextArea | Session summary
Session_Window__c | Lookup(Session_Window__c) | Session Window
Severity__c | Picklist | Severity | values: critical; high; medium; low
Start_Time_IST__c | Text | Start Time IST
Status__c | Picklist | Status | values: Unscheduled; Scheduled; Completed; Rescheduled; Cancelled
Time_Zone__c | Picklist | Time Zone | globalValueSet: Candidate_Time_Zone
Trainer_Coverage_Pct__c | Percent | Trainer coverage %
Trainer_Name__c | Text | Trainer name
Trainer_On_Camera_Pct__c | Percent | Trainer on-camera %
Trainer_Talk_Pct__c | Percent | Trainer talk %
Training_Day_Number__c | Number | Training day
URL__c | LongTextArea | URL
```

## Session_Attendee__c - Session Attendee (11 fields)

```
Candidate__c | Lookup(Account) | Candidate | required
Candidate_Training__c | Lookup(Candidate_Training__c) | Candidate Training | required
Candidate_Training_Step__c | Lookup(CandidateTrainingStep__c) | Candidate Training Step | required
Candidate_TZ_End_Time__c | Formula<Text> | Candidate TZ End Time
Candidate_TZ_Start_Time__c | Formula<Text> | Candidate TZ Start Time
Meeting_Link__c | Formula<Text> | Meeting Link
Meeting_Topic__c | Formula<Text> | Meeting Topic
Scheduled_Date__c | Formula<Date> | Scheduled Date
Session__c | MasterDetail(Session__c) | Session
Session_Status__c | Formula<Text> | Session Status
Time_Zone__c | Formula<Text> | Time Zone
```

## Session_Window__c - Session Window (13 fields)

```
Activate_From__c | Date | Activate From
Booked_Count__c | Number | Booked Count
Candidate_Capacity__c | Number | Candidate Capacity
Cohort__c | Lookup(Cohort__c) | Cohort
End_Time__c | Time | End Time | required
Program_Version__c | MasterDetail(Program_Version__c) | Program Version
Sequence__c | Number | Sequence | required
Start_Date__c | Date | Start Date
Start_Time__c | Time | Start Time | required
Status__c | Picklist | Status | values: Upcoming; Active; Completed
Time_Zone__c | Picklist | Time Zone | globalValueSet: Candidate_Time_Zone
Trainer__c | Lookup(Recruiter__c) | Trainer
Window_Label__c | Text | Window Label | required
```

## Step_Deliverable_Definition__c - Step Deliverable Definition (8 fields)

```
Days_To_Complete__c | Number | Days To Complete
Is_Required__c | Checkbox | Is Required
Key__c | AutoNumber | Key
Label__c | Text | Label | required
Lookup_Key__c | Text | Lookup Key
Schema_JSON__c | LongTextArea | Schema JSON
Step_Template__c | Lookup(StepTemplate__c) | Step Template
Type__c | Picklist | Type | values: Video_URL; File; URL; Text; Checkbox; Image
```

## StepTemplate__c - Step Template (14 fields)

```
Description__c | LongTextArea | Description
Expected_Duration_Min__c | Number | Expected Duration Min
Is_Required__c | Checkbox | Is Required
Learning_Goal__c | LongTextArea | Learning Goal
Lookup_Key__c | Text | Lookup Key
Program__c | Lookup(Program__c) | Program
Program_Version__c | MasterDetail(Program_Version__c) | Program Version
Sequence__c | Number | Sequence
Step_Description__c | Html | Step Description
Tags__c | Text | Tags
Title__c | Text | Title | required
Training_Method__c | LongTextArea | Training Notes
Training_Step__c | LongTextArea | Training Step
Triggers_Mock__c | Checkbox | Triggers Mock
```

## Template__c - Template (3 fields)

```
Interview_Type__c | Lookup(Interview_Type__c) | Interview Type
Is_Active__c | Checkbox | Is Active
Niche__c | Lookup(Niche__c) | Niche
```

## Template_Section__c - Template Section (4 fields)

```
Is_Active__c | Checkbox | Is Active
Section__c | Lookup(Section__c) | Section
Sequence__c | Number | Sequence
Template__c | MasterDetail(Template__c) | Template
```

## Template_Section_Question__c - Template Section Question (4 fields)

```
Is_Active__c | Checkbox | Is Active
Question__c | Lookup(Question_Bank__c) | Question
Sequence__c | Number | Sequence
Template_Section__c | MasterDetail(Template_Section__c) | Template Section
```

## Vendor__c - Vendor (5 fields)

```
Email__c | Email | Email
In_House_Proxy__c | Checkbox | In House Proxy
Phone__c | Phone | Phone
Slack_User_Id_c__c | Text | Slack User Id
Vendor_Parent__c | Lookup(Vendor__c) | Vendor (Parent)
```

## Zoom_Recording_Access_Log__c - Zoom Recording Access Log (19 fields)

```
Department__c | Lookup(Zoom_Recording_Department__c) | Department
Error_Message__c | LongTextArea | Error Message
Event_Timestamp__c | DateTime | Event Timestamp
Event_Type__c | Picklist | Event Type | values: View; Download; Access_Denied; Login; Share; Portal_Access; Logout; Signed_URL; File_Access; Sync_Complete; Sync_Error; Shared_Link_Accessed; Video_Shared; Login_Success; Screen_Recording_Attempt; Portal_Exit; Transcript_Copy
File_Key__c | Text | File Key
File_Name__c | Text | File Name
Interview__c | Lookup(Interview__c) | Interview
IP_Address__c | Text | IP Address
Opened_At__c | DateTime | Opened At
Opened_By__c | Lookup(User) | Opened By
Record_Id__c | Text | Record Id
Record_Name__c | Text | Record Name
Recording_Link__c | Url | Recording Link
Session__c | Lookup(Session__c) | Session
Source__c | Picklist | Source | values: App; Experience Cloud
Source_Object__c | Picklist | Source Object | values: interview; session
Status__c | Picklist | Status | values: Success; Failed; Blocked
User__c | Lookup(User) | User
User_Agent__c | TextArea | User Agent
```

## Zoom_Recording_Department__c - Zoom Recording Department (6 fields)

```
Department_Code__c | Text | Department Code
Description__c | TextArea | Description
Display_Order__c | Number | Display Order
Folder_Path_S3__c | Text | Folder Path (S3)
Is_Active__c | Checkbox | Is Active
Permission_Set_API_Name__c | Text | Permission Set API Name
```

## Zoom_Recording_User_Folder_Map__c - Zoom Recording User Folder Map (9 fields)

```
Department__c | Lookup(Zoom_Recording_Department__c) | Department | required
Folder_Name__c | Text | Folder Name | required
Granted_By__c | Lookup(User) | Granted By
Granted_Date__c | DateTime | Granted Date
Is_Active__c | Checkbox | Is Active
Notes__c | LongTextArea | Notes
S3_Folder_Path__c | Text | S3 Folder Path | required | externalId
User__c | Lookup(User) | User
User_Email__c | Formula<Text> | User Email
```

---

# Standard Objects

## Account - Account (194 fields)

```
Account_Manager__c | Lookup(User) | Account Manager
AccountNumber |  | 
AccountSource | Picklist | 
Acknowledgement_Declaration__c | Checkbox | Acknowledgement Declaration
Acquaintances_Using_Our_Services__c | Text | Acquaintances Using Our Services
Active_CPT__c | Checkbox | Active CPT?
Active_for_B2B_Submission__c | Checkbox | Active for B2B Submission
Additional_Comments_or_Questions__c | LongTextArea | Additional Comments or Questions
Additional_Information__c | LongTextArea | Additional Information
Address_Line_1__c | Text | Address Line 1
Address_Line_2__c | Text | Address Line 2
AnnualRevenue |  | 
Application_Start_Date__c | Date | Application Start Date
Assigned_Marketing_Team_Lead__c | Lookup(Recruiter__c) | Assigned Marketing Team Lead
Assigned_Recruiter__c | Lookup(Recruiter__c) | Assigned Recruiter
Authorized_To_Work_In_US__c | Checkbox | Authorized To Work In US?
BillingAddress |  | 
Candidate_Owner_Name__c | Formula<Text> | Candidate Owner Name
Candidate_Referral__c | Lookup(Account) | Candidate Referral
Candidate_Status__c | Picklist | Candidate Status | values: In Progress; Active; Hold; Paused; Placed; Terminate; Closed
Candidate_Status_Change_Reason__c | LongTextArea | Candidate Status Change Reason
Client_Status__c | Picklist | Client Status | values: Prospect; Active; On Hold; Inactive
Client_Type__c | Picklist | Client Type | values: Direct Client; Vendor; Implementation Partner; MSP; Other
College_Name__c | Text | College Name
College_Name_2__c | Text | College Name 2
College_Name_3__c | Text | College Name 3
College_Name_4__c | Text | College Name 4
College_Name_5__c | Text | College Name 5
Communication_Skills_Rating__c | Number | Communication Skills Rating
Complete_Address__c | Formula<Text> | Complete Address
Confidential_Client__c | Checkbox | Confidential Client
Contact_Number_Calling__c | Phone | Contact Number Calling
Contact_Number_WhatsApp__c | Phone | Contact Number WhatsApp
Criminal_Record_Declaration__c | Picklist | Criminal Record Declaration | values: I do NOT have any criminal record; I have a criminal record
Current_Address__c | Address | Current Address
Current_Job_Position__c | Text | Current Job Position
Current_Rate_Expectation__c | Currency | Current Rate Expectation
Current_Visa_Status__c | Picklist | Current Visa Status | values: CPT; OPT; STEM OPT Extension; H1B; B2; Green Card EAD; Green Card / Permanent Resident; US Citizen
Currently_Employed__c | Checkbox | Currently Employed
Date_of_Arrival__c | Date | Date of Arrival
Date_of_Birth__c | Date | Date of Birth
Day_to_day_availability_for_interviews__c | Text | Day to day availability for interviews
Days_since_application_started__c | Formula<Number> | Days since application started
Default_Submission_Email__c | Email | Default Submission Email
Default_Vendor__c | Lookup(Vendor__c) | Default Vendor
Degree__c | Picklist | Degree | values: Undergraduates; Graduates; Bachelors; Masters
Degree_End_Date__c | Date | Degree End Date
Degree_End_Date_2__c | Date | Degree End Date 2
Degree_End_Date_3__c | Date | Degree End Date 3
Degree_End_Date_4__c | Date | Degree End Date 4
Degree_End_Date_5__c | Date | Degree End Date 5
Degree_Start_Date__c | Date | Degree Start Date
Degree_Start_Date_2__c | Date | Degree Start Date 2
Degree_Start_Date_3__c | Date | Degree Start Date 3
Degree_Start_Date_4__c | Date | Degree Start Date 4
Degree_Start_Date_5__c | Date | Degree Start Date 5
Description |  | 
Description__c | LongTextArea | Description
Disability_Status__c | Picklist | Disability Status | values: Yes, I have a disability or previously had a disability; No, I do not have a disability; Prefer not to say
DS_Account_Type__c | Picklist | DS Account Type | values: Checking; Savings
DS_ACH_Identification_Number__c | Text | DS ACH Identification Number
DS_ACH_Identification_Type__c | Picklist | DS ACH Identification Type | values: Passport; Driver's License; State Id
DS_ACH_Issuing_Authority__c | Text | DS ACH Issuing Authority
DS_Address_Line_1__c | Text | DS Address Line 1
DS_Address_Line_2__c | Text | DS Address Line 2
DS_Bank_Name__c | Text | DS Bank Name
DS_City__c | Text | DS City
DS_Date_Of_Birth__c | Date | DS Date Of Birth
DS_First_Name__c | Text | DS First Name
DS_Last_Name__c | Text | DS Last Name
DS_Middle_Name__c | Text | DS Middle Name
DS_Person_Email__c | Email | DS Person Email
DS_Person_Phone__c | Phone | DS Person Phone
DS_Service_Identification_Number__c | Text | DS Service Identification Number
DS_Service_Identification_Type__c | Picklist | DS Service Identification Type | values: Passport; Driver's License; State Id
DS_Service_Issuing_Authority__c | Text | DS Service Issuing Authority
DS_State_Province__c | Picklist | DS State/Province | globalValueSet: State
DS_Today_Date__c | Formula<Text> | DS Today Date
DS_ZIP_Postal_Code__c | Text | DS ZIP/Postal Code
Email_replica__c | Formula<Text> | Email_replica
Employee_Referral__c | Text | Employee Referral
Expected_Interviews_Target__c | Formula<Number> | Expected Interviews (Target)
Expected_No_of_Interviews__c | Formula<Number> | Expected No of Interviews
Fax |  | 
Home_Address__c | Address | Home Address
Industry | Picklist | 
Internal_Interview__c | Lookup(Internal_Interview__c) | Internal Interview
Interview_Support__c | Checkbox | Interview Support
Interviews_Ghosted__c | Summary | Interviews Ghosted
Interviews_Rejected__c | Summary | Interviews Rejected
IsCustomerPortal |  | 
January_Kicker_Rate__c | Percent | January Kicker Rate
Jigsaw |  | 
Job_Seeking_Intensity__c | Number | Job Seeking Intensity
Joining_Date__c | Date | Joining Date
Last_4_digits_of_SSN__c | Number | Last 4 digits of SSN
Last_Alert_Sent__c | Date | Last Alert Sent
Latest_Internal_Interview_Date__c | DateTime | Latest Internal Interview Date
LinkedIn_Email_Id__c | Email | LinkedIn Email Id
LinkedIn_Password__c | EncryptedText | LinkedIn Password
LinkedIn_Profile_URL__c | Url | LinkedIn Profile URL
List_of_Certifications__c | TextArea | List of Certifications
Marketing_Email_Id__c | Email | Marketing Email Id
Marketing_Email_Password__c | EncryptedText | Marketing Email Password
Middle_Name__c | Text | Middle Name
Military_Status__c | Picklist | Military Status | values: Active Duty; Reserve or National Guard; Veteran; Retired Military; Military Spouse; No Military Service; Prefer not to say
Name |  | 
Niche__c | Picklist | Niche | values: AI ML Engineer; Python Developer/Engineer; Java Fullstack Developer/Engineer; Product/Project Manager; Marketing Manager; Finance Analyst; Other
Niche_Other__c | Text | Niche Other
No_of_Degrees__c | Picklist | No of Degrees | values: 1; 2; 3; 4; 5
Notes__c | TextArea | Notes
Notice_Period__c | Picklist | Notice Period | values: Immediate; 1 Week; 2 Weeks; 30 Days; 60 Days
NumberOfEmployees |  | 
Offer_letter_amount__c | Currency | Offer letter amount
Open_to_relocate__c | Checkbox | Open to relocate
Other_Consultancies_Name__c | Text | Other Consultancies Name
Other_please_specify__c | Text | Other (please specify)
OwnerId | Lookup | 
Ownership | Picklist | 
ParentId | Hierarchy | 
Passport_Number__c | Text | Passport Number
Payment_Terms__c | Picklist | Payment Terms | values: Net 15; Net 30; Net 45; Net 60
PersonAssistantName |  | 
PersonAssistantPhone |  | 
PersonBirthdate |  | 
PersonDepartment |  | 
PersonDoNotCall |  | 
PersonEmail |  | 
PersonGenderIdentity | Picklist | 
PersonHasOptedOutOfEmail |  | 
PersonHasOptedOutOfFax |  | 
PersonHomePhone |  | 
PersonLastCURequestDate |  | 
PersonLastCUUpdateDate |  | 
PersonLeadSource | Picklist | 
PersonMailingAddress |  | 
PersonMobilePhone |  | 
PersonOtherAddress |  | 
PersonOtherPhone |  | 
PersonPronouns | Picklist | 
PersonTitle |  | 
Phone |  | 
Plan__c | Picklist | Plan | values: Career  Launcher; Career Accelerator; Premium / Fastrack; Ultimate Career Architect
Position_Type_C2C__c | Checkbox | Position Type C2C
Position_Type_Full_Time_CTH__c | Checkbox | Position Type Full Time CTH
Preferred_Job_Locations__c | Text | Preferred Job Locations
Preferred_Job_Positions__c | Text | Preferred Job Positions
Preferred_Job_Type__c | Picklist | Preferred Job Type | values: Remote; Hybrid; On-site
Preferred_Locations__c | TextArea | Preferred Locations
Preferred_Tech_stack_If_Applicable__c | Text | Preferred Tech stack (If Applicable)
Preferred_Training_Schedule_EST__c | Picklist | Preferred Training Schedule EST | values: 9:30 AM â€“ 11:30 AM EST; 11:30 AM â€“ 1:30 PM EST; 2:30 PM â€“ 4:30 PM EST; 4:30 PM â€“ 6:30 PM EST
Preferred_Work_Mode__c | Picklist | Preferred Work Mode | values: "Remote; Hybrid; Onsite"
Previously_Engaged_with_Other_Consultanc__c | Checkbox | Previously Engaged with Other Consultanc
Profile_Notes__c | Html | Profile Notes
Project_Understanding_Document__c | LongTextArea | Project Understanding Document
QB_Customer_ID__c | Text | QB Customer ID | externalId
Race_Ethnicity__c | Picklist | Race/Ethnicity | values: American Indian or Alaska Native; Asian; Black or African American; Hispanic or Latino; Native Hawaiian or Other Pacific Islander; White; Two or More Races; Other (please specify)
Rating | Picklist | 
Reason_for_Hold__c | LongTextArea | Reason for Hold
Relocation_Other__c | Text | Relocation Other
Relocation_Readiness__c | Picklist | Relocation Readiness | values: Yes, I am willing to relocate anywhere in the USA; Yes, but only within specific states (mention in comments); No, I prefer remote / local positions only; Other
Resume_Ready__c | Checkbox | Resume Ready
Salary__c | Percent | Salary %
Salary_Expectations__c | Text | Salary Expectations
Secondary_Email__c | Email | Secondary Email
Secondary_Phone__c | Text | Secondary Phone
Select_Employee__c | Lookup(Recruiter__c) | Select Employee
Service_Agreement_Sent__c | Checkbox | Service Agreement Sent?
ShippingAddress |  | 
Sic |  | 
SicDesc |  | 
Site |  | 
Slack_User_Id__c | Text | Slack User Id | unique | externalId
SourceSystemIdentifier |  | 
Sponsorship_Required_For_VISA__c | Checkbox | Sponsorship Required For VISA?
Target_Achieved__c | Formula<Percent> | Target Achieved
Targeted_Experience_Level__c | Picklist | Targeted Experience Level | values: Entry Level; Associate; Mid-Senior level; Director; Executive
Technology__c | Picklist | Technology | values: Account Manager; AI/ML Engineer; Automation Engineer; AWS DevOps Engineer; AWS Solutions Architect; Azure DevOps Engineer; Business & Strategy Consultant; Business Analyst; Cloud Engineer; Computer Support Associate; Customer Success; Cybersecurity Engineer; Data Engineer; DevOps Engineer; Director of Engineering; Electrical Engineer; Engineering Manager; Financial Analyst; Frontend UI Developer; Full Stack Developer; Healthcare Informatics; Human Resources; Industrial Engineer; IT Project Manager; Java Developer; Layout Engineer; Lead Software Engineer; Management Consultant; Manufacturing Engineer; Mechanical Engineer; Microsoft Dynamics Consultant; Network Security Engineer; Oracle/SQL Developer; Product Manager; Product Owner & Scrum Master; QA Automation Lead; Remote IT Technician; Risk Analyst; Sales; Salesforce Administrator; Salesforce Developer; SDET / Automation Tester; Senior Front-End Developer; Software Engineer; System Administrator; Technical Project Manager; VP of Engineering; Python Developer; UI/UX Designer; QA Engineer; Supply Chain Management
TickerSymbol |  | 
Tier |  | 
Total_Internal_Interviews_Excl_Intake__c | Number | Total Internal Interviews Excl Intake
Total_Interviews__c | Number | Total Interviews
Total_no_of_Interviews_Initial_calls__c | Summary | Total no. of Interviews & Initial calls
Total_Number_of_Applications__c | Number | Total Number of Applications
Total_Number_of_Initial_Calls__c | Summary | Total Number of Initial Calls
Total_Number_of_Interview__c | Summary | Total Number of Interviews
Total_Number_of_Interviews__c | Number | Total Number of Interviews
Total_Training_Sessions__c | Number | Total Training Sessions
Total_Years_of_Experience__c | Number | Total Years of Experience
Type | Picklist | 
Upfront_Amount__c | Currency | Upfront Amount
Website |  | 
Work_Authorization__c | Picklist | Work Authorization | globalValueSet: Work_Authorization_Picklist
Work_Authorization_Expiry_Date__c | Date | Work Authorization Expiry Date
```

## Case - Case (47 fields)

```
AccountId | Lookup | 
AssetId | Lookup | 
BusinessHoursId | Lookup | 
Case_Owner_Name__c | Formula<Text> | Case Owner Name
Case_Reason__c | Picklist | Case Reason | values: Resume Edit; LinkedIn Edit; Experience Adjustment; Start; Pause; Resume; Priority Change; Warning (Responsiveness); Availability; Missed Session; Skill Gap; Track Change; Inactive / No Response; Seriousness / Commitment; Direct Bypass (Text/Email); Recording Request; Support Mandatory; Scheduling; Proxy Exception (One-off); Signature Pending; Payment Pending; Plan Upgrade; Policy Clarification; Legal Warning; Policy Breach; Escalation; Evidence Request; New Referral
Case_Tags__c | MultiselectPicklist | Case Tags | values: C2C; Full-Time; Urgent; Agreement Breach Risk; Referral; Delay; Support Exception; Payment Pending; Smart Team Directive; Training
ClosedDate |  | 
Comments |  | 
ContactEmail |  | 
ContactFax |  | 
ContactId | Lookup | 
ContactMobile |  | 
ContactPhone |  | 
Deadline__c | Date | Deadline | required
Description |  | 
EntitlementId | Lookup | 
Interview__c | Lookup(Interview__c) | Interview
IsClosedOnCreate |  | 
IsEscalated |  | 
IsStopped |  | 
Language |  | 
Last_Priority_Change_Reason__c | LongTextArea | Last Priority Change Reason
Marketing__c | Lookup(Marketing__c) | Marketing
MilestoneStatus |  | 
MilestoneStatusIcon |  | 
Next_Action_Date__c | Date | Next Action Date
Onboarding__c | Lookup(Onboarding__c) | Onboarding
Origin | Picklist | 
OwnerId | Lookup | 
ParentId | Lookup | 
Priority | Picklist | 
ProductId | Lookup | 
Reason | Picklist | 
ServiceContractId | Lookup | 
Slack_Thread_Ts__c | Text | Slack Thread Ts | unique | externalId
SlaExitDate |  | 
SlaStartDate |  | 
SourceId | Lookup | 
Status | Picklist | 
StopStartDate |  | 
Subject |  | 
SuppliedCompany |  | 
SuppliedEmail |  | 
SuppliedName |  | 
SuppliedPhone |  | 
Type | Picklist | 
Waiting_On__c | Picklist | Waiting On | values: Candidate; Recruiter; Trainer; Support; Legal; Vendor/Client; Internal Review
```

## Contact - Contact (35 fields)

```
AccountId | Lookup | 
AssistantName |  | 
AssistantPhone |  | 
Birthdate |  | 
BuyerAttributes | Picklist | 
Contact_Role__c | Picklist | Contact Role | values: Hiring Manager; HR; Vendor Manager; Technical Panel; Recruiter; Finance; Other
ContactSource |  | 
Department |  | 
DepartmentGroup |  | 
Description |  | 
DoNotCall |  | 
Email |  | 
Fax |  | 
GenderIdentity | Picklist | 
HasOptedOutOfEmail |  | 
HasOptedOutOfFax |  | 
HomePhone |  | 
Jigsaw |  | 
LastCURequestDate |  | 
LastCUUpdateDate |  | 
LeadSource | Picklist | 
MailingAddress |  | 
MobilePhone |  | 
Name |  | 
OtherAddress |  | 
OtherPhone |  | 
OwnerId | Lookup | 
Phone |  | 
Primary_Submission_Contact__c | Checkbox | Primary Submission Contact
Pronouns | Picklist | 
Receives_Submission_Emails__c | Checkbox | Receives Submission Emails
ReportsToId | Lookup | 
Submission_Notes__c | LongTextArea | Submission Notes
Title |  | 
TitleType |  | 
```

## ContentDocument - ContentDocument (11 fields)

```
ArchivedById | Lookup | 
ArchivedDate |  | 
ContentAssetId | Lookup | 
DeletedById | Lookup | 
DeletedDate |  | 
IsArchived |  | 
IsInternalOnly |  | 
OwnerId | Lookup | 
ParentId | Lookup | 
PublishStatus |  | 
Title |  | 
```

## ContentVersion - ContentVersion (19 fields)

```
ContentSize |  | 
ContentSizeLong |  | 
CreatedBynameFormula__c | Formula<Text> | CreatedBynameFormula
Description |  | 
dfsle__GeneratedFileFormat__c | Picklist | Generated File Format | values: Word; PDF
dfsle__GeneratedFileName__c | Text | Generated File Name
dfsle__GeneratedFileSuffix__c | Picklist | Generated File Suffix | values: name; date; name_date
dfsle__Rule__c | LongTextArea | Rule
File_Type__c | Picklist | File Type | values: Resume; Service Agreement
FileExtension |  | 
FileType |  | 
Guest_Record_fileupload__c | Text | Guest Record
IsAssetEnabled |  | 
Language |  | 
OwnerId | Lookup | 
SharingOption |  | 
SharingPrivacy |  | 
TagCsv |  | 
Title |  | 
```

## EmailMessage - EmailMessage (23 fields)

```
AutomationType |  | 
BccAddress |  | 
CcAddress |  | 
FirstOpenedDate |  | 
FromAddress |  | 
FromName |  | 
HasAttachment |  | 
Headers |  | 
HtmlBody |  | 
Incoming |  | 
IsExternallyVisible |  | 
IsPrivateDraft |  | 
LastOpenedDate |  | 
MessageDate |  | 
MessageSize |  | 
ParentId | Lookup | 
RelatedToId | Lookup | 
Source |  | 
Status |  | 
Subject |  | 
TextBody |  | 
ToAddress |  | 
ValidatedFromAddress |  | 
```

## Event - Event (22 fields)

```
ActivityDate |  | 
ActivityDateTime |  | 
Attendees |  | 
Description |  | 
DurationInMinutes |  | 
Email |  | 
EndDateTime |  | 
EventSubtype |  | 
IsAllDayEvent |  | 
IsPrivate |  | 
IsRecurrence2 |  | 
IsReminderSet |  | 
IsVisibleInSelfService |  | 
Location |  | 
OwnerId | Lookup | 
Phone |  | 
ShowAs |  | 
StartDateTime |  | 
Subject | Picklist | 
Type | Picklist | 
WhatId | Lookup | 
WhoId | Lookup | 
```

## FeedItem - FeedItem (0 fields)

_No retrievable fields._

## Lead - Lead (88 fields)

```
Acknowledgement_Declaration__c | Checkbox | Acknowledgement Declaration
Additional_Comments_or_Questions__c | LongTextArea | Additional Comments or Questions
Address |  | 
AnnualRevenue |  | 
Applied_Job_Requirement__c | Lookup(Job_Requirement__c) | Applied Job Requirement
B2B_Company_Type__c | Picklist | B2B Company Type | values: Direct Client; Vendor; Implementation Partner; Consulting Partner
CampaignId | Lookup | 
Candidate_Rate_Expectation__c | Currency | Candidate Rate Expectation
Company |  | 
Company_Email__c | Email | Company Email
Company_Name__c | Lookup(Company__c) | Company Name
Company_Type__c | Formula<Text> | Company Type
Contact_Number_WhatsApp__c | Phone | Contact Number WhatsApp
Conversion_Status__c | Formula<Text> | Conversion Status
Converted_to_Job_Requirement__c | Checkbox | Converted to Job Requirement
Criminal_Record_Declaration__c | Picklist | Criminal Record Declaration | values: I do NOT have any criminal record; I have a criminal record
Current_Visa_Status__c | Picklist | Current Visa Status | globalValueSet: Visa_Status
Department__c | Text | Department
Description |  | 
Designation__c | Text | Designation
DoNotCall |  | 
Email |  | 
Email__c | Email | Email
Estimated_Bill_Rate__c | Currency | Estimated Bill Rate
Fax |  | 
First_Name__c | Text | First Name
Flow_Only_Field__c | Checkbox | Flow Only Field
GenderIdentity | Picklist | 
HasOptedOutOfEmail |  | 
HasOptedOutOfFax |  | 
Header_Display__c | Formula<Text> | Lead - Company
Industry | Picklist | 
Jigsaw |  | 
Last_Name__c | Text | Last Name
LastTransferDate |  | 
Latest_Pre_Enrolment_Form_Link__c | Url | Latest Pre Enrolment Form Link
Latest_Pre_Enrolment_Opened_On__c | DateTime | Latest Pre Enrolment Opened On
Latest_Pre_Enrolment_Sent_On__c | DateTime | Latest Pre Enrolment Sent On
Latest_Pre_Enrolment_Status__c | Text | Latest Pre Enrolment  Status
Latest_Pre_Enrolment_Submitted_On__c | DateTime | Latest Pre Enrolment Submitted On
Lead_Category__c | Picklist | Lead Category | values: Candidate; B2B Client
Lead_Status__c | Picklist | Lead Status (not use) | values: New; Contacted; Negotiation; Agreement; Converted; Lost; Junk
LeadSource | Picklist | 
LinkedIn_URL__c | Url | LinkedIn URL
MobilePhone |  | 
Name |  | 
Niche__c | Picklist | Niche | globalValueSet: Niche
Niche_Other__c | Text | Niche Other
NumberOfEmployees |  | 
Offer_letter_amount__c | Currency | Offer letter amount
OwnerId | Lookup | 
Payment_Completed__c | Checkbox | Payment Completed
Phone |  | 
Plan__c | Picklist | Plan | values: Career Accelerator; Ultimate Career Architect
Position_Type_C2C__c | Checkbox | Position Type C2C
Position_Type_Full_Time_CTH__c | Checkbox | Position Type Full Time CTH
Pre_Enrolment_Request__c | Lookup(Pre_Enrolment_Request__c) | Current Pre Enrolment Request
Preferred_Work_Mode__c | Picklist | Preferred Work Mode | values: Remote; Hybrid; Onsite
Primary_Technology__c | TextArea | Primary Technology
Processed_for_JS__c | Checkbox | Processed for JS
Pronouns | Picklist | 
QB_Customer_ID__c | Text | QB Customer ID | externalId
Rating | Picklist | 
Rating__c | Picklist | Rating | values: 1; 2; 3; 4; 5
Relocation_Other__c | TextArea | Relocation Other
Relocation_Readiness__c | Picklist | Relocation Readiness | values: Yes, I am willing to relocate anywhere in the USA; Yes, but only within specific states (mention in comments); No, I prefer remote / local positions only; Other
Requirement_Summary__c | LongTextArea | Requirement Summary
Resume_Attached__c | Checkbox | Resume Attached
Resume_Link__c | Url | Resume Link
Salary__c | Percent | Salary %
State__c | Picklist | State | globalValueSet: State
Status | Picklist | 
Submission_Deadline__c | DateTime | Submission Deadline
Target_Role__c | Text | Target Role
Target_Role_n__c | Formula<Text> | Target Role
Technology__c | TextArea | Technology
Title |  | 
Total_Job_Applications__c | Number | Total Job Applications
Total_Job_Requirements__c | Number | Total Job Requirements
Training_Schedule__c | Picklist | Training Schedule | globalValueSet: Training_Schedule
Upfront_amount__c | Currency | Upfront Amount
Visa_Status__c | Picklist | Visa Status | globalValueSet: Visa_Status
Website |  | 
Website_Job_External_ID__c | TextArea | Website Job External ID
Work_Authorization__c | Picklist | Work Authorization | globalValueSet: Work_Authorization_Picklist
Years_of_Experience__c | Number | Years of Experience
Zip_Code__c | Text | Zip Code
ZVC__IsCreatedByZoomApp__c | Checkbox | IsCreatedByZoomApp
```

## Opportunity - Opportunity (24 fields)

```
AccountId | Lookup | 
Amount |  | 
Budget_Confirmed__c | Checkbox | Budget Confirmed
CampaignId | Lookup | 
CloseDate |  | 
ContractId | Lookup | 
Description |  | 
Discovery_Completed__c | Checkbox | Discovery Completed
ExpectedRevenue |  | 
IqScore |  | 
IsPrivate |  | 
LeadSource | Picklist | 
Loss_Reason__c | Picklist | Loss Reason | values: Lost to Competitor; No Budget / Lost Funding; No Decision / Non-Responsive; Price; Other
Name |  | 
NextStep |  | 
OwnerId | Lookup | 
pandadoc__TrackingNumber__c | Text | Tracking Number
Pricebook2Id | Lookup | 
Probability |  | 
ROI_Analysis_Completed__c | Checkbox | ROI Analysis Completed
StageName | Picklist | 
SyncedQuoteId | Lookup | 
TotalOpportunityQuantity |  | 
Type | Picklist | 
```

## QuickText - QuickText (6 fields)

```
Category | Picklist | 
Channel | Picklist | 
IsInsertable |  | 
Message |  | 
Name |  | 
OwnerId | Lookup | 
```

## Site - Site (0 fields)

_No retrievable fields._

## Task - Task (22 fields)

```
ActivityDate |  | 
CallDisposition |  | 
CallDurationInSeconds |  | 
CallObject |  | 
CallType |  | 
CompletedDateTime |  | 
Description |  | 
Email |  | 
IsRecurrence |  | 
IsReminderSet |  | 
IsVisibleInSelfService |  | 
OwnerId | Lookup | 
Phone |  | 
Priority | Picklist | 
RecurrenceInterval |  | 
RecurrenceRegeneratedType |  | 
Status | Picklist | 
Subject | Picklist | 
TaskSubtype |  | 
Type | Picklist | 
WhatId | Lookup | 
WhoId | Lookup | 
```

## User - User (50 fields)

```
AboutMe |  | 
Address |  | 
Alias |  | 
CallCenterId | Lookup | 
CommunityNickname |  | 
CompanyName |  | 
ContactId | Lookup | 
DefaultGroupNotificationFrequency |  | 
DelegatedApproverId | Lookup | 
Department |  | 
dfsle__CanManageAccount__c | Checkbox | Can Manage DocuSign Account
dfsle__Provisioned__c | Date | DocuSign Provisioned Date
dfsle__Status__c | Picklist | Docusign Status | values: Inactive; Pending; Active
dfsle__Username__c | Text | DocuSign Username
DigestFrequency |  | 
Division |  | 
Email |  | 
EmailEncodingKey |  | 
EmployeeNumber |  | 
EndDay |  | 
Extension |  | 
Fax |  | 
FederationIdentifier |  | 
ForecastEnabled |  | 
IsActive |  | 
IsSystemControlled |  | 
LanguageLocaleKey |  | 
LocaleSidKey |  | 
ManagerId | Hierarchy | 
MobilePhone |  | 
Name |  | 
Phone |  | 
PortalRole |  | 
ProfileId | Lookup | 
ReceivesAdminInfoEmails |  | 
ReceivesInfoEmails |  | 
SenderEmail |  | 
SenderName |  | 
Signature |  | 
Slack_User_Id__c | Text | Slack User Id
StartDay |  | 
StayInTouchNote |  | 
StayInTouchSignature |  | 
StayInTouchSubject |  | 
TimeZoneSidKey |  | 
Title |  | 
Username |  | 
UserRoleId | Lookup | 
UserSubtype |  | 
WorkspaceId | Lookup | 
```

---

# Custom Metadata Types

## Follow_Up_Setting__mdt - Follow Up Setting (2 fields)

```
Max_Reminders__c | Number | Max Reminders
Reminder_Interval_Days__c | Number | Reminder Interval Days
```

## Interview_Incentive_Configuration__mdt - Interview Incentive Configuration (5 fields)

```
Incentivized__c | Checkbox | Incentivized
Interview_Round__c | Picklist | Interview Round | values: Initial; First; Second; Third; Fourth; Fifth; Sixth; Seventh; Eighth; Ninth; Tenth; Eleventh; Twelfth; Thirteenth; Fourteenth; Fifteenth; Final
Negative_Incentive_Rate__c | Number | Negative Incentive Rate
Positive_Incentive_Rate__c | Number | Positive Incentive Rate
Round_Multiplier__c | Number | Round Multiplier
```

## Slack_Email_Notification_Metadata__mdt - Slack & Email Notification Metadata (7 fields)

```
Is_Current__c | Checkbox | Is Current
Org_Id__c | Text | Org Id | required
Reminder_Channel_Id__c | Text | slack_channel_followup_reminders
Sender_Email__c | Email | Sender Email
Slack_App_Id__c | Text | Slack App Id
Slack_Workspace_Id__c | Text | Slack Workspace Id
Support_Reminder_Channel_Id__c | Text | Support Reminder Channel Id
```

## Token_Secret__mdt - Token Secret (1 fields)

```
Value__c | Text | Value
```

## Zoom_Portal_Config__mdt - Zoom Portal Config (9 fields)

```
Allowed_File_Extensions__c | Text | Allowed File Extensions
AWS_Access_Key__c | Text | AWS Access Key
AWS_Region__c | Text | AWS Region | required
AWS_Secret_Key__c | Text | AWS Secret Key
Is_Active__c | Checkbox | Is Active
S3_Bucket_Name__c | Text | S3 Bucket Name | required
Signed_URL_Expiry_Minutes__c | Number | Signed URL Expiry Minutes
Site_Title__c | Text | Site Title
Top_Notice__c | LongTextArea | Top Notice
```

---

# Knowledge Articles

## Knowledge__kav - Knowledge (0 fields)

_No retrievable fields._


