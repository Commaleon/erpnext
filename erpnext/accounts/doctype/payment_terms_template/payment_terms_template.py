# Copyright (c) 2017, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt


import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt


class PaymentTermsTemplate(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from erpnext.accounts.doctype.payment_terms_template_detail.payment_terms_template_detail import (
			PaymentTermsTemplateDetail,
		)

		allocate_payment_based_on_payment_terms: DF.Check
		template_name: DF.Data | None
		terms: DF.Table[PaymentTermsTemplateDetail]
	# end: auto-generated types

	def validate(self):
		self.validate_invoice_portion()
		self.validate_terms()
		self.validate_procurement_release()

	def validate_procurement_release(self):
		if self.procurement_release != "After required payments":
			return

		if not self.allocate_payment_based_on_payment_terms:
			frappe.throw(
				_("Enable Allocate Payment Based On Payment Terms for automatic procurement after payment.")
			)

		terms = [row.payment_term for row in self.terms]
		required = [row for row in self.terms if row.required_before_procurement]
		if (
			len(terms) != len(set(terms))
			or not required
			or any(flt(row.invoice_portion) <= 0 for row in required)
		):
			frappe.throw(
				_(
					"Use unique payment terms and mark at least one positive installment as required before procurement."
				)
			)

	def validate_invoice_portion(self):
		total_portion = 0
		for term in self.terms:
			total_portion += flt(term.get("invoice_portion", 0))

		if flt(total_portion, 2) != 100.00:
			frappe.msgprint(_("Combined invoice portion must equal 100%"), raise_exception=1, indicator="red")

	def validate_terms(self):
		terms = []
		for term in self.terms:
			if self.allocate_payment_based_on_payment_terms and not term.payment_term:
				frappe.throw(_("Row {0}: Payment Term is mandatory").format(term.idx))

			term_info = (term.payment_term, term.credit_days, term.credit_months, term.due_date_based_on)
			if term_info in terms:
				frappe.msgprint(
					_("The Payment Term at row {0} is possibly a duplicate.").format(term.idx),
					raise_exception=1,
					indicator="red",
				)
			else:
				terms.append(term_info)
