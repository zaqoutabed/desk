import json
from email.utils import parseaddr
from functools import lru_cache
from typing import List

import frappe
from frappe import _
from frappe.desk.form.assign_to import add as assign
from frappe.desk.form.assign_to import clear as clear_all_assignments
from frappe.model.document import Document
from frappe.query_builder import Case, DocType, Order
from pypika.functions import Count
from pypika.queries import Query
from pypika.terms import Criterion

from helpdesk.consts import DEFAULT_TICKET_PRIORITY, DEFAULT_TICKET_TYPE
from helpdesk.helpdesk.doctype.hd_ticket_activity.hd_ticket_activity import (
	log_ticket_activity,
)
from helpdesk.helpdesk.utils.email import (
	default_outgoing_email_account,
	default_ticket_outgoing_email_account,
)
from helpdesk.utils import capture_event, get_customer, is_agent, publish_event

from ..hd_service_level_agreement.utils import get_sla


class HDTicket(Document):
	@staticmethod
	def get_list_select(query: Query):
		QBTicket = frappe.qb.DocType("HD Ticket")
		QBComment = frappe.qb.DocType("HD Ticket Comment")
		QBCommunication = frappe.qb.DocType("Communication")

		count_comment = (
			frappe.qb.from_(QBComment)
			.select(Count("*"))
			.as_("count_comment")
			.where(QBComment.reference_ticket == QBTicket.name)
		)

		count_msg_incoming = (
			frappe.qb.from_(QBCommunication)
			.select(Count("*"))
			.as_("count_msg_incoming")
			.where(QBCommunication.reference_doctype == "HD Ticket")
			.where(QBCommunication.reference_name == QBTicket.name)
			.where(QBCommunication.sent_or_received == "Received")
		)

		count_msg_outgoing = (
			frappe.qb.from_(QBCommunication)
			.select(Count("*"))
			.as_("count_msg_outgoing")
			.where(QBCommunication.reference_doctype == "HD Ticket")
			.where(QBCommunication.reference_name == QBTicket.name)
			.where(QBCommunication.sent_or_received == "Sent")
		)

		query = (
			query.select(QBTicket.star)
			.select(count_comment)
			.select(count_msg_incoming)
			.select(count_msg_outgoing)
		)

		return query

	@staticmethod
	def get_list_filters(query: Query):
		QBTeam = frappe.qb.DocType("HD Team")
		QBTeamMember = frappe.qb.DocType("HD Team Member")
		QBTicket = frappe.qb.DocType("HD Ticket")
		user = frappe.session.user
		customer = get_customer(user)
		conditions = (
			[
				QBTicket.contact == user,
				QBTicket.customer == customer,
				QBTicket.raised_by == user,
			]
			if not is_agent()
			else []
		)
		query = query.where(Criterion.any(conditions))

		enable_restrictions, ignore_restrictions = frappe.get_value(
			doctype="HD Settings",
			fieldname=[
				"restrict_tickets_by_agent_group",
				"do_not_restrict_tickets_without_an_agent_group",
			],
		)
		enable_restrictions = bool(int(enable_restrictions))
		ignore_restrictions = bool(int(ignore_restrictions))

		if not enable_restrictions:
			return query

		teams = (
			frappe.qb.from_(QBTeamMember)
			.where(QBTeamMember.user == user)
			.join(QBTeam)
			.on(QBTeam.name == QBTeamMember.parent)
			.select(QBTeam.team_name, QBTeam.ignore_restrictions)
			.run(as_dict=True)
		)

		can_ignore_restrictions = (
			len(list(filter(lambda x: x.ignore_restrictions, teams))) > 0
		)

		if can_ignore_restrictions:
			return query

		conditions = [QBTicket.agent_group == team.team_name for team in teams]

		# Consider tickets without any assigned agent group
		if ignore_restrictions:
			conditions.append(QBTicket.agent_group.isnull())

		query = query.where(Criterion.any(conditions))
		return query

	@staticmethod
	@lru_cache
	def sort_options():
		def by_priority(query: Query, direction: Order):
			QBTicket = frappe.qb.DocType("HD Ticket")
			QBPriority = frappe.qb.DocType("HD Ticket Priority")

			query = (
				query.left_join(QBPriority)
				.on(QBPriority.name == QBTicket.priority)
				.orderby(QBPriority.integer_value, order=direction)
				.orderby(QBTicket.resolution_by, order=Order.desc)
			)

			return query

		return {
			"Due date": ("resolution_by", Order.asc),
			"Created on": ("creation", Order.asc),
			"High to low priority": lambda q: by_priority(q, Order.asc),
			"Low to high priority": lambda q: by_priority(q, Order.desc),
			"Last modified on": "modified",
		}

	def publish_update(self):
		publish_event("helpdesk:ticket-update", {"name": self.name})
		capture_event("ticket_updated")

	def autoname(self):
		return self.name

	def get_feed(self):
		return "{0}: {1}".format(_(self.status), self.subject)

	def before_validate(self):
		self.check_update_perms()
		self.set_ticket_type()
		self.set_raised_by()
		self.set_contact()
		self.set_customer()
		self.set_priority()
		self.apply_escalation_rule()
		self.set_sla()

	def validate(self):
		self.validate_feedback()
		self.validate_ticket_type()

	def before_save(self):
		self.apply_sla()

	def after_insert(self):
		log_ticket_activity(self.name, "created this ticket")
		capture_event("ticket_created")
		publish_event("helpdesk:new-ticket", {"name": self.name})

	def on_update(self):
		self.handle_ticket_activity_update()
		self.remove_assignment_if_not_in_team()
		self.publish_update()

	def set_ticket_type(self):
		if self.ticket_type:
			return
		settings = frappe.get_doc("HD Settings")
		ticket_type = settings.default_ticket_type or DEFAULT_TICKET_TYPE
		self.ticket_type = ticket_type

	def set_raised_by(self):
		self.raised_by = self.raised_by or frappe.session.user

	def set_contact(self):
		email_id = parseaddr(self.raised_by)[1]
		if email_id:
			if not self.contact:
				contact = frappe.db.get_value("Contact", {"email_id": email_id})
				if contact:
					self.contact = contact

	def set_customer(self):
		"""
		Update `Customer` if does not exist already. `Contact` is assumed
		to be set beforehand.
		"""
		# Skip if `Customer` is already set
		if self.customer:
			return
		self.customer = get_customer(self.contact)

	def set_priority(self):
		if self.priority:
			return
		self.priority = (
			frappe.get_cached_value("HD Ticket Type", self.ticket_type, "priority")
			or frappe.get_cached_value("HD Settings", "HD Settings", "default_priority")
			or DEFAULT_TICKET_PRIORITY
		)

	def validate_ticket_type(self):
		settings = frappe.get_doc("HD Settings")
		if settings.is_ticket_type_mandatory and not self.ticket_type:
			frappe.throw(_("Ticket type is mandatory"))

	def validate_feedback(self):
		if (
			self.feedback
			or self.status != "Resolved"
			or not self.has_value_changed("status")
			or is_agent()
		):
			return
		frappe.throw(
			_("Ticket must be resolved with a feedback"), frappe.ValidationError
		)

	def check_update_perms(self):
		if self.is_new() or is_agent():
			return
		old_doc = self.get_doc_before_save()
		is_closed = old_doc.status == "Closed"
		is_rated = bool(old_doc.feedback)
		if is_closed or is_rated:
			text = _("Closed or rated tickets cannot be updated by non-agents")
			frappe.throw(text, frappe.PermissionError)

	def handle_ticket_activity_update(self):
		"""
		Handles the ticket activity update.
		Should be called inside on_update
		"""
		field_maps = {
			"status": "status",
			"priority": "priority",
			"agent_group": "team",
			"ticket_type": "type",
			"contact": "contact",
		}
		for field in [
			"status",
			"priority",
			"agent_group",
			"contact",
			"ticket_type",
		]:
			if self.has_value_changed(field):
				log_ticket_activity(
					self.name, f"set {field_maps[field]} to {self.as_dict()[field]}"
				)

	def remove_assignment_if_not_in_team(self):
		"""
		Removes the assignment if the agent is not in the team.
		Should be called inside on_update
		"""
		if self.has_value_changed("agent_group") and self.status == "Open":
			current_assigned_agent_doc = self.get_assigned_agent()
			if (
				current_assigned_agent_doc
				and not current_assigned_agent_doc.in_group(self.agent_group)
			) and frappe.get_doc(
				"Assignment Rule",
				frappe.get_doc("HD Team", self.agent_group).assignment_rule,
			).users:
				clear_all_assignments("HD Ticket", self.name)
				frappe.publish_realtime(
					"helpdesk:update-ticket-assignee",
					{"ticket_id": self.name},
					after_commit=True,
				)

	def create_communication(self):
		communication = frappe.new_doc("Communication")
		communication.update(
			{
				"communication_type": "Communication",
				"communication_medium": "Email",
				"sent_or_received": "Received",
				"email_status": "Open",
				"subject": self.subject,
				"sender": self.raised_by,
				"content": self.description,
				"status": "Linked",
				"reference_doctype": "HD Ticket",
				"reference_name": self.name,
			}
		)
		communication.ignore_permissions = True
		communication.ignore_mandatory = True
		communication.save(ignore_permissions=True)

	@frappe.whitelist()
	def assign_agent(self, agent):
		if not agent:
			return

		if self._assign:
			assignees = json.loads(self._assign)
			for assignee in assignees:
				if agent == assignee:
					# the agent is already set as an assignee
					return

		clear_all_assignments("HD Ticket", self.name)
		assign({"assign_to": [agent], "doctype": "HD Ticket", "name": self.name})
		publish_event("helpdesk:ticket-assignee-update", {"name": self.name})

	def get_assigned_agent(self):
		# for some reason _assign is not set, maybe a framework bug?
		if hasattr(self, "_assign") and self._assign:
			assignees = json.loads(self._assign)
			if len(assignees) > 0:
				agent_doc = frappe.get_doc("HD Agent", assignees[0])
				return agent_doc

		from frappe.desk.form.assign_to import get

		assignees = get({"doctype": "HD Ticket", "name": self.name})
		if len(assignees) > 0:
			agent_doc = frappe.get_doc("HD Agent", assignees[0].owner)
			return agent_doc

		return None

	def on_trash(self):
		activities = frappe.db.get_all("HD Ticket Activity", {"ticket": self.name})
		for activity in activities:
			frappe.db.delete("HD Ticket Activity", activity)

	def skip_email_workflow(self):
		skip: str = frappe.get_value("HD Settings", None, "skip_email_workflow") or "0"

		return bool(int(skip))

	def instantly_send_email(self):
		check: str = (
			frappe.get_value("HD Settings", None, "instantly_send_email") or "0"
		)

		return bool(int(check))

	@frappe.whitelist()
	def get_last_communication(self):
		filters = {"reference_doctype": "HD Ticket", "reference_name": ["=", self.name]}

		try:
			communication = frappe.get_last_doc(
				"Communication",
				filters=filters,
			)

			return communication
		except Exception:
			pass

	def last_communication_email(self):
		if not (communication := self.get_last_communication()):
			return

		if not communication.email_account:
			return

		email_account = frappe.get_doc("Email Account", communication.email_account)

		if not email_account.enable_outgoing:
			return

		return email_account

	def sender_email(self):
		"""
		Find an email to use as sender. Fall back through multiple choices

		:return: `Email Account`
		"""
		if email_account := self.last_communication_email():
			return email_account

		if email_account := default_ticket_outgoing_email_account():
			return email_account

		if email_account := default_outgoing_email_account():
			return email_account

	@property
	def portal_uri(self):
		root_uri = frappe.utils.get_url()
		return f"{root_uri}/helpdesk/my-tickets/{self.name}"

	@frappe.whitelist()
	def new_comment(self, content: str):
		if not is_agent():
			frappe.throw(
				_("You are not permitted to add a comment"), frappe.PermissionError
			)
		c = frappe.new_doc("HD Ticket Comment")
		c.commented_by = frappe.session.user
		c.content = content
		c.is_pinned = False
		c.reference_ticket = self.name
		c.save()

	@frappe.whitelist()
	def reply_via_agent(
		self, message: str, cc: str = None, bcc: str = None, attachments: List[str] = []
	):
		skip_email_workflow = self.skip_email_workflow()
		medium = "" if skip_email_workflow else "Email"
		subject = f"Re: {self.subject} (#{self.name})"
		sender = frappe.session.user
		recipients = self.raised_by
		sender_email = None if skip_email_workflow else self.sender_email()
		last_communication = self.get_last_communication()

		if last_communication:
			cc = cc or last_communication.cc
			bcc = bcc or last_communication.bcc

		if recipients == "Administrator":
			admin_email = frappe.get_value("User", "Administrator", "email")
			recipients = admin_email

		communication = frappe.get_doc(
			{
				"bcc": bcc,
				"cc": cc,
				"communication_medium": medium,
				"communication_type": "Communication",
				"content": message,
				"doctype": "Communication",
				"email_account": sender_email.name if sender_email else None,
				"email_status": "Open",
				"recipients": recipients,
				"reference_doctype": "HD Ticket",
				"reference_name": self.name,
				"sender": sender,
				"sent_or_received": "Sent",
				"status": "Linked",
				"subject": subject,
			}
		)

		communication.insert(ignore_permissions=True)
		capture_event("agent_replied")

		if skip_email_workflow:
			return

		if not sender_email:
			frappe.throw(_("Can not send email. No sender email set up!"))

		_attachments = []

		for attachment in attachments:
			file_doc = frappe.get_doc("File", attachment)
			file_doc.attached_to_name = communication.name
			file_doc.attached_to_doctype = "Communication"
			file_doc.save(ignore_permissions=True)
			_attachments.append({"file_url": file_doc.file_url})

		reply_to_email = sender_email.email_id
		template = (
			"new_reply_on_customer_portal_notification"
			if self.via_customer_portal
			else None
		)
		args = {
			"message": message,
			"portal_link": self.portal_uri,
			"ticket_id": self.name,
		}
		send_delayed = True
		send_now = False

		if self.instantly_send_email():
			send_delayed = False
			send_now = True

		try:
			frappe.sendmail(
				args=args,
				attachments=_attachments,
				bcc=bcc,
				cc=cc,
				communication=communication.name,
				delayed=send_delayed,
				expose_recipients="header",
				message=message,
				now=send_now,
				recipients=recipients,
				reference_doctype="HD Ticket",
				reference_name=self.name,
				reply_to=reply_to_email,
				sender=reply_to_email,
				subject=subject,
				template=template,
				with_container=False,
			)
		except Exception as e:
			frappe.throw(_(e))

	@frappe.whitelist()
	def create_communication_via_contact(self, message, attachments=[]):
		if self.status == "Replied":
			self.status = "Open"
			log_ticket_activity(self.name, "set status to Open")
			self.save(ignore_permissions=True)

		c = frappe.new_doc("Communication")
		c.communication_type = "Communication"
		c.communication_medium = "Email"
		c.sent_or_received = "Received"
		c.email_status = "Open"
		c.subject = "Re: " + self.subject
		c.sender = frappe.session.user
		c.content = message
		c.status = "Linked"
		c.reference_doctype = "HD Ticket"
		c.reference_name = self.name
		c.ignore_permissions = True
		c.ignore_mandatory = True
		c.save(ignore_permissions=True)

		if not len(attachments):
			return
		QBFile = frappe.qb.DocType("File")
		condition_name = [QBFile.name == i["name"] for i in attachments]
		frappe.qb.update(QBFile).set(QBFile.attached_to_name, c.name).set(
			QBFile.attached_to_doctype, "Communication"
		).where(Criterion.any(condition_name)).run()

	@frappe.whitelist()
	def mark_seen(self):
		self.add_view()
		self.add_seen()

	def add_view(self):
		d = frappe.new_doc("View Log")
		d.reference_doctype = "HD Ticket"
		d.reference_name = self.name
		d.viewed_by = frappe.session.user
		d.insert(ignore_permissions=True)

	@frappe.whitelist()
	def get_assignees(self):
		QBUser = DocType("User")
		assignees = frappe.parse_json(self._assign)

		if not assignees:
			return []

		condition = [QBUser.name == assignee for assignee in assignees]

		res = (
			frappe.qb.from_(QBUser)
			.select(QBUser.name, QBUser.full_name, QBUser.user_image)
			.where(Case.any(condition))
			.run(as_dict=True)
		)

		return res

	@frappe.whitelist()
	def get_communications(self):
		conversations = frappe.db.get_all(
			"Communication",
			filters={
				"reference_doctype": ["=", "HD Ticket"],
				"reference_name": ["=", self.name],
			},
			order_by="creation asc",
			fields=[
				"name",
				"content",
				"creation",
				"sent_or_received",
				"sender",
				"cc",
				"bcc",
			],
		)

		for conversation in conversations:
			if frappe.db.exists("HD Agent", conversation.sender):
				# user User details instead of Contact if the sender is an agent
				sender = frappe.get_doc("User", conversation.sender).__dict__
				sender["image"] = sender["user_image"]
			else:
				contacts = frappe.get_all(
					"Contact Email",
					filters=[["email_id", "like", "%{0}".format(conversation.sender)]],
					fields=["parent"],
					limit=1,
				)
				if len(contacts) > 0:
					sender = frappe.get_doc("Contact", contacts[0].parent)
				else:
					sender = frappe.get_last_doc(
						"User", filters={"email": conversation.sender}
					)

			conversation.sender = sender

			attachments = frappe.get_all(
				"File",
				["file_name", "file_url"],
				{
					"attached_to_name": conversation.name,
					"attached_to_doctype": "Communication",
				},
			)

			conversation.attachments = attachments

		return conversations

	def get_escalation_rule(self):
		filters = [
			{
				"priority": self.priority,
				"team": self.agent_group,
				"ticket_type": self.ticket_type,
			},
			{
				"priority": self.priority,
				"team": self.agent_group,
			},
			{
				"priority": self.priority,
				"ticket_type": self.ticket_type,
			},
			{
				"team": self.agent_group,
				"ticket_type": self.ticket_type,
			},
			{
				"priority": self.priority,
			},
			{
				"team": self.agent_group,
			},
			{
				"ticket_type": self.ticket_type,
			},
		]

		for i in range(len(filters)):
			try:
				f = {
					**filters[i],
					"is_enabled": True,
				}
				rule = frappe.get_last_doc("HD Escalation Rule", filters=f)
				if rule:
					return rule
			except Exception:
				pass

	def apply_escalation_rule(self):
		if not self.status == "Open" or self.is_new():
			return
		escalation_rule = self.get_escalation_rule()
		if not escalation_rule:
			return
		self.agent_group = escalation_rule.to_team or self.agent_group
		self.priority = escalation_rule.to_priority or self.priority
		self.ticket_type = escalation_rule.to_ticket_type or self.ticket_type
		self.assign_agent(escalation_rule.to_agent)

	def set_sla(self):
		if sla := get_sla(self):
			self.sla = sla.name

	def apply_sla(self):
		sla = frappe.get_doc("HD Service Level Agreement", self.sla)
		sla.apply(self)
