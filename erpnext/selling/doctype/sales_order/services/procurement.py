# Copyright (c) Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

import json
from collections import defaultdict

import frappe
from frappe import _
from frappe.query_builder.functions import Coalesce, NullIf, Sum
from frappe.utils import flt, nowdate

INACTIVE_STATUSES = ("Closed", "Completed", "On Hold", "Cancelled")
MANUAL_REVIEW = "Manual review"
AFTER_PAYMENTS = "After required payments"
WITHOUT_ADVANCE = "Without advance"


class SalesOrderProcurement:
	def __init__(self, order):
		self.order = order

	def capture_payment_policy(self):
		order = self.order
		order.procurement_payment_policy = None
		if not order.payment_terms_template or not self.warehouses():
			return

		template = frappe.get_cached_doc("Payment Terms Template", order.payment_terms_template)
		mode = template.procurement_release or MANUAL_REVIEW
		if mode == MANUAL_REVIEW:
			return
		required_terms = []
		if mode == AFTER_PAYMENTS:
			required_terms = [row.payment_term for row in template.terms if row.required_before_procurement]
			schedule = [row.payment_term for row in order.payment_schedule]
			if not required_terms or any(schedule.count(term) != 1 for term in required_terms):
				frappe.throw(
					_(
						"Each required procurement payment term must appear once in the order's payment schedule."
					)
				)

		order.procurement_payment_policy = json.dumps({"mode": mode, "required_terms": required_terms})

	def validate_payment_policy(self):
		before = self.order.get_doc_before_save()
		if before and (before.procurement_payment_policy or "") != (
			self.order.procurement_payment_policy or ""
		):
			frappe.throw(_("The procurement payment policy saved at submission cannot be changed."))

	def payment_released(self):
		policy = json.loads(self.order.procurement_payment_policy or "{}")
		if policy.get("mode") == WITHOUT_ADVANCE:
			return True
		if policy.get("mode") != AFTER_PAYMENTS or not policy.get("required_terms"):
			return False

		schedule = defaultdict(list)
		for row in self.order.payment_schedule:
			schedule[row.payment_term].append(row)

		for term in policy["required_terms"]:
			rows = schedule[term]
			if (
				len(rows) != 1
				or rows[0].outstanding is None
				or flt(rows[0].outstanding, rows[0].precision("outstanding")) > 0
			):
				return False
		return True

	def warehouses(self):
		warehouses = {
			row.warehouse
			for row in self.order.items
			if row.warehouse and not row.closed and not row.delivered_by_supplier
		}
		if not warehouses or self.order.is_subcontracted:
			return set()
		return set(
			frappe.get_all(
				"Warehouse",
				filters={
					"name": ("in", sorted(warehouses)),
					"company": self.order.company,
					"is_group": 0,
					"disabled": 0,
					"warehouse_type": ("!=", "Transit"),
					"auto_create_purchase_material_requests": 1,
				},
				pluck="name",
			)
		)

	def enqueue(self):
		order = self.order
		if (
			order.docstatus == 1
			and order.status not in INACTIVE_STATUSES
			and order.procurement_payment_policy
			and self.warehouses()
		):
			frappe.enqueue(
				create_purchase_material_request,
				enqueue_after_commit=True,
				company=order.company,
				sales_order=order.name,
			)

	def create_request(self):
		from erpnext.selling.doctype.sales_order.mapper import _map_material_request

		order = self.order
		if order.docstatus != 1 or order.status in INACTIVE_STATUSES or not self.payment_released():
			return

		warehouses = self.warehouses()
		if not warehouses:
			return

		needed = _shortage_quantities(order, warehouses)
		if not needed:
			return

		request = _map_material_request(order.name, shortages=needed)
		request.transaction_date = nowdate()
		request.material_request_type = "Purchase"
		# This warehouse setting authorizes the system-generated request even when
		# the user submitting a Sales Order or Payment Entry has no Buying role.
		request.insert(ignore_permissions=True)
		request.submit()
		return request.name


def enqueue_payment_orders(payment):
	references = {
		row.reference_name
		for row in payment.references
		if row.reference_doctype == "Sales Order" and row.reference_name
	}
	if not references:
		return

	for name in frappe.get_all(
		"Sales Order",
		filters={
			"name": ("in", sorted(references)),
			"company": payment.company,
			"docstatus": 1,
			"procurement_payment_policy": ("is", "set"),
		},
		pluck="name",
	):
		SalesOrderProcurement(frappe.get_doc("Sales Order", name)).enqueue()


def create_purchase_material_request(company: str, sales_order: str):
	# Demand for the same stock can span orders. Serialize workers before reading demand and supply.
	if not frappe.db.get_value("Company", company, "name", for_update=True):
		return
	try:
		order = frappe.get_doc("Sales Order", sales_order, for_update=True)
	except frappe.DoesNotExistError:
		return
	if order.company != company:
		return

	return SalesOrderProcurement(order).create_request()


def _shortage_quantities(order, warehouses):
	lines = {row.name: row for row in order.items if row.warehouse in warehouses}
	items = sorted({row.item_code for row in lines.values()})
	warehouses = sorted(warehouses)
	open_lines = _get_open_sales_order_items(order.company, items, warehouses)
	if not open_lines:
		return {}

	line_keys = {row.name: (row.item_code, row.warehouse) for row in open_lines}
	reserved = _get_reserved_quantities(line_keys, warehouses)
	coverage, held, shared = _get_purchase_coverage(order.company, items, warehouses, line_keys)
	available = _get_available_stock(items, warehouses)
	needed = {}
	for row in open_lines:
		key = (row.item_code, row.warehouse)
		open_qty = flt(row.stock_qty) - max(0, flt(row.delivered_qty)) * flt(row.conversion_factor)
		uncovered = max(0, open_qty - coverage[row.name] - reserved.get((row.name, row.warehouse), 0))
		from_stock = min(uncovered, available[key])
		available[key] -= from_stock
		if row.name in lines and row.name not in held and key not in shared:
			quantity = flt(uncovered - from_stock, lines[row.name].precision("stock_qty"))
			if quantity > 0:
				needed[row.name] = quantity
	return needed


def _get_open_sales_order_items(company, items, warehouses):
	so = frappe.qb.DocType("Sales Order")
	row = frappe.qb.DocType("Sales Order Item")
	item = frappe.qb.DocType("Item")
	return (
		frappe.qb.from_(row)
		.join(so)
		.on(so.name == row.parent)
		.join(item)
		.on(item.name == row.item_code)
		.select(
			row.name, row.item_code, row.warehouse, row.stock_qty, row.delivered_qty, row.conversion_factor
		)
		.where(
			(so.company == company)
			& (so.docstatus == 1)
			& so.status.notin(INACTIVE_STATUSES)
			& (so.is_subcontracted == 0)
			& (row.closed == 0)
			& (row.delivered_by_supplier == 0)
			& (item.is_stock_item == 1)
			& row.item_code.isin(items)
			& row.warehouse.isin(warehouses)
		)
		.orderby(so.creation, so.name, row.idx, row.name)
		.run(as_dict=True)
	)


def _get_reserved_quantities(line_keys, warehouses):
	sre = frappe.qb.DocType("Stock Reservation Entry")
	rows = (
		frappe.qb.from_(sre)
		.select(
			sre.voucher_detail_no,
			sre.warehouse,
			Sum(sre.reserved_qty - sre.delivered_qty - sre.transferred_qty - sre.consumed_qty),
		)
		.where(
			(sre.docstatus == 1)
			& (sre.voucher_type == "Sales Order")
			& sre.voucher_detail_no.isin(sorted(line_keys))
			& sre.warehouse.isin(warehouses)
		)
		.groupby(sre.voucher_detail_no, sre.warehouse)
		.run()
	)
	return {(line, warehouse): max(0, flt(qty)) for line, warehouse, qty in rows}


def _get_available_stock(items, warehouses):
	rows = frappe.get_all(
		"Bin",
		filters={"item_code": ("in", items), "warehouse": ("in", warehouses)},
		fields=[
			"item_code",
			"warehouse",
			"actual_qty",
			"reserved_stock",
			"reserved_qty_for_production",
			"reserved_qty_for_sub_contract",
			"reserved_qty_for_production_plan",
		],
	)
	return defaultdict(
		float,
		{
			(row.item_code, row.warehouse): max(
				0,
				flt(row.actual_qty)
				- flt(row.reserved_stock)
				- flt(row.reserved_qty_for_production)
				- flt(row.reserved_qty_for_sub_contract)
				- flt(row.reserved_qty_for_production_plan),
			)
			for row in rows
		},
	)


def _get_purchase_coverage(company, items, warehouses, line_keys):
	coverage = defaultdict(float)
	held = set()
	shared = set()
	mr = frappe.qb.DocType("Material Request")
	mri = frappe.qb.DocType("Material Request Item")
	requests = (
		frappe.qb.from_(mri)
		.join(mr)
		.on(mr.name == mri.parent)
		.select(
			mri.sales_order_item,
			mri.item_code,
			mri.warehouse,
			mri.stock_qty,
			mri.ordered_qty,
			mri.received_qty,
			mr.docstatus,
		)
		.where(
			(mr.company == company)
			& (mr.material_request_type == "Purchase")
			& mr.docstatus.isin((0, 1))
			& (mr.status != "Stopped")
			& mri.sales_order_item.isin(sorted(line_keys))
			& mri.warehouse.isin(warehouses)
		)
		.run(as_dict=True)
	)
	for row in requests:
		if line_keys[row.sales_order_item] != (row.item_code, row.warehouse):
			continue
		if row.docstatus == 0:
			held.add(row.sales_order_item)
		else:
			coverage[row.sales_order_item] += max(
				0, flt(row.stock_qty) - max(flt(row.ordered_qty), flt(row.received_qty))
			)

	po = frappe.qb.DocType("Purchase Order")
	poi = frappe.qb.DocType("Purchase Order Item")
	purchases = (
		frappe.qb.from_(poi)
		.join(po)
		.on(po.name == poi.parent)
		.left_join(mri)
		.on(mri.name == poi.material_request_item)
		.select(
			Coalesce(NullIf(poi.sales_order_item, ""), mri.sales_order_item).as_("sales_order_item"),
			poi.item_code,
			poi.warehouse,
			poi.stock_qty,
			poi.received_qty,
			poi.conversion_factor,
			po.docstatus,
		)
		.where(
			(po.company == company)
			& po.docstatus.isin((0, 1))
			& po.status.notin(("Closed", "Stopped", "On Hold"))
			& poi.item_code.isin(items)
			& poi.warehouse.isin(warehouses)
		)
		.run(as_dict=True)
	)
	for row in purchases:
		remaining = max(0, flt(row.stock_qty) - flt(row.received_qty) * flt(row.conversion_factor))
		if line_keys.get(row.sales_order_item) == (row.item_code, row.warehouse):
			if row.docstatus == 0:
				held.add(row.sales_order_item)
			else:
				coverage[row.sales_order_item] += remaining
		elif not row.sales_order_item and row.docstatus == 1 and remaining:
			# Shared incoming stock needs an explicit allocation before automatic purchasing.
			shared.add((row.item_code, row.warehouse))
	return coverage, held, shared
