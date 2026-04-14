Perfect 👌
👉 I’m going to give you a clean, professional, developer-ready English version of your SFD (Functional Specs) and STD (Technical Specs) — fully aligned with:
	•	your estimator
	•	your catalogue logic
	•	your AI chatbot (expert in renovation)
	•	your appointment booking system
	•	your Eurobat positioning (modern renovation approach)

⸻

📘 SFD — Functional Specifications Document

1. Objective

The goal is to build a digital platform (Eurobat) that allows users to:
	•	estimate a renovation project
	•	receive an automated pre-quote (pre-estimate)
	•	interact with an AI assistant specialized in renovation
	•	ask questions and get expert guidance
	•	book appointments directly
	•	move from estimation → qualification → conversion

⸻

2. Product Vision

Eurobat is not a traditional renovation company.

It is a modern, structured, and intelligent renovation platform designed to:
	•	simplify decision-making
	•	provide early cost visibility
	•	reduce uncertainty
	•	guide users through their renovation project

The system is built around two core pillars:

⸻

A. Estimator (Core Engine)

Responsible for:
	•	selecting services
	•	calculating price ranges (min/max)
	•	generating a pre-quote
	•	sending it via email
	•	creating a qualified lead

⸻

B. AI Chatbot Assistant (Renovation Expert)

A key component of the product.

Responsible for:
	•	understanding user intent in natural language
	•	answering renovation-related questions
	•	explaining services and concepts
	•	guiding users to the estimator
	•	proposing appointments
	•	booking meetings in the admin calendar
	•	assisting after the pre-quote is sent

⸻

3. Target Users

End User (Customer)

Can:
	•	explore the site
	•	estimate a renovation project
	•	receive a pre-quote
	•	ask questions via chatbot
	•	book an appointment

Admin (Main Operator)

Can:
	•	receive leads
	•	access estimations
	•	receive appointment requests
	•	manage calendar
	•	convert pre-quote → final quote

⸻

4. User Journey

Journey 1 — Direct Estimation
	1.	User clicks “Estimate my project”
	2.	Selects services
	3.	Inputs quantities
	4.	Gets estimated price range
	5.	Provides contact details
	6.	Receives pre-quote by email
	7.	Chatbot may follow up or propose appointment

⸻

Journey 2 — Chatbot First
	1.	User opens chatbot
	2.	Describes project
	3.	Chatbot qualifies the need
	4.	Redirects to estimator OR suggests booking

⸻

Journey 3 — Post-Estimate Engagement
	1.	User receives pre-quote
	2.	Has questions
	3.	Chatbot explains
	4.	Chatbot proposes appointment
	5.	User books meeting

⸻

5. Estimator Functional Requirements

5.1 Catalogue-based system

The estimator must rely on the official Eurobat catalogue.

Each item includes:
	•	lot
	•	service
	•	sub-service
	•	code
	•	unit (m2, m3, unit, forfait)
	•	calculation mode
	•	min price
	•	max price

⸻

5.2 Service selection

User can select multiple services:
	•	full renovation
	•	kitchen installation
	•	plumbing
	•	flooring
	•	electrical
	•	etc.

⸻

5.3 Quantity input

Depending on unit:
	•	m2 → surface
	•	m3 → volume
	•	unit → number
	•	forfait → fixed (no input required)

⸻

5.4 Calculation

System computes:
	•	line min price
	•	line max price
	•	total min
	•	total max

⸻

5.5 Pre-quote generation

System generates:
	•	project summary
	•	selected services
	•	quantities
	•	total price range
	•	disclaimer

⸻

5.6 Contact capture

Required fields:
	•	name
	•	email
	•	phone
	•	location
	•	optional message

⸻

5.7 Email delivery

Pre-quote sent to:
	•	customer
	•	optionally admin

⸻

5.8 Lead creation

System stores:
	•	customer info
	•	selected services
	•	estimated price
	•	timestamp
	•	source = estimator

⸻

6. Chatbot Functional Requirements

6.1 Role

The chatbot is not a generic chatbot.

It must behave as a:
👉 Renovation & construction expert assistant

⸻

6.2 Capabilities

Understand user intent

Examples:
	•	“I want to renovate my apartment”
	•	“How much does a bathroom cost?”
	•	“What is included in a full renovation?”

⸻

Provide expert answers
	•	explain services
	•	explain price ranges
	•	explain process
	•	explain differences (light vs full renovation)

⸻

Guide user
	•	to estimator
	•	to appointment
	•	to next step

⸻

Propose appointments
	•	suggest available slots
	•	allow user to choose
	•	confirm booking

⸻

Book appointment
	•	connect to admin calendar
	•	create event
	•	send confirmation

⸻

Introduce 3D visualization (optional service)

Must be presented as:
	•	optional
	•	premium
	•	“on request”

⚠️ Never push price directly

⸻

6.3 Constraints

Chatbot must NOT:
	•	calculate prices itself
	•	invent services
	•	give final quotes
	•	override backend logic

⸻

7. Business Rules (Estimator)

Units

Unit	Rule
m2	quantity × price
m3	quantity × price
unit	quantity × price
forfait	fixed price


⸻

Price logic

Always compute:
	•	total_min
	•	total_max

⸻

Validation
	•	quantity required (except forfait)
	•	quantity > 0
	•	service must exist
	•	service must be active

⸻

⸻

⚙️ STD — Technical Specifications Document

⸻

1. Architecture

Frontend (Website + Estimator + Chatbot)
        ↓
FastAPI Backend
        ↓
Core Modules:
- Catalogue
- Estimation Engine
- Chatbot Orchestrator
- Calendar Module
- Email Module
        ↓
PostgreSQL Database
        ↓
External Services:
- LLM API
- Email provider
- Google Calendar API


⸻

2. Core Modules

2.1 Estimation Engine
	•	reads catalogue
	•	applies calculation rules
	•	returns min/max

⸻

2.2 Catalogue Service
	•	stores all services
	•	serves frontend + chatbot

⸻

2.3 Chatbot Orchestrator
	•	connects LLM to backend
	•	detects intent
	•	calls correct endpoints

⸻

2.4 Calendar Service
	•	retrieves available slots
	•	creates bookings

⸻

2.5 Email Service
	•	sends pre-quotes
	•	sends confirmations

⸻

3. Database Models

catalog_services

id
lot
service
sub_service
code
unit
mode_calcul
price_min
price_max
is_active


⸻

customers

id
name
email
phone
city


⸻

estimates

id
customer_id
total_min
total_max
status
created_at


⸻

estimate_lines

id
estimate_id
service_code
quantity
unit
line_total_min
line_total_max


⸻

appointments

id
customer_id
estimate_id
start_at
end_at
status
calendar_event_id


⸻

chat_messages

id
conversation_id
role
content
intent
created_at


⸻

4. API Endpoints

Estimator

GET /catalog/services
POST /estimates/calculate
POST /estimates
GET /estimates/{id}


⸻

Chatbot

POST /chat/message


⸻

Calendar

GET /calendar/slots
POST /calendar/book


⸻

5. Estimation Logic

if mode == "m2" or mode == "m3" or mode == "unit":
    total = quantity * price
elif mode == "forfait":
    total = price


⸻

6. Chatbot Architecture

Layer 1 — LLM
	•	language understanding
	•	conversation

Layer 2 — Backend
	•	business logic
	•	data access

Layer 3 — Data
	•	catalogue
	•	estimates
	•	appointments

⸻

7. Chatbot Intents
	•	request_estimate
	•	ask_service_info
	•	ask_price_info
	•	ask_process
	•	request_appointment
	•	confirm_booking
	•	ask_about_3d

⸻

8. Calendar Flow
	1.	User requests appointment
	2.	API returns available slots
	3.	User selects slot
	4.	API creates booking
	5.	Confirmation sent

⸻

9. Security Rules
	•	backend validates all calculations
	•	frontend never sends price
	•	catalogue is source of truth
	•	API secured
	•	logs enabled

⸻

10. Implementation Roadmap

Phase 1
	•	estimator + calculation
	•	email pre-quote

Phase 2
	•	chatbot expert renovation
	•	estimator integration

Phase 3
	•	calendar booking

Phase 4
	•	automation + follow-up

⸻

✅ FINAL NOTE FOR DEVELOPERS

You can include this sentence:

👉
“The chatbot must be implemented as an AI assistant specialized in renovation and construction, capable of understanding user needs, explaining services, guiding users to the estimator, and booking appointments in the admin calendar. All pricing logic must remain handled exclusively by the backend and the official catalogue.”

⸻

If you want next step, I can give you:
👉 FastAPI project structure (folders + code starter)
👉 or database schema ready for PostgreSQL migration

Just tell me 👍