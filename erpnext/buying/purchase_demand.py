# Copyright (c) 2026, Commaleon and Contributors
# License: GNU General Public License v3. See license.txt

"""Separate linked purchase demand from the quantity physically bought or received."""

from decimal import Decimal

import frappe
from frappe import _

FLAG = "purchase_demand_managed"
QTY = "purchase_demand_qty"


def configured_warehouses(company):
	config = frappe.conf.get("erpnext_purchase_demand") or {}
	if not config.get("enabled") or config.get("company") != company:
		return ()
	if (frappe.conf.get("commaleon_procurement") or {}).get("enabled"):
		frappe.throw(_("Disable Commaleon Ops procurement before enabling ERPNext purchase demand tracking."))
	return tuple(config.get("warehouses") or ())


def number(value):
	return Decimal(str(value or 0))


def effective_quantity(row):
	return number(row.get(QTY) if row.get(FLAG) else row.get("stock_qty"))


def clear_demand_fields(doc, method=None):
	if doc.docstatus == 0 or doc._action == "submit":
		for row in doc.items:
			row.set(FLAG, 0)
			row.set(QTY, 0)


def source_expression():
	return f"CASE WHEN {FLAG}=1 THEN {QTY} ELSE stock_qty END"


def _received_demand(purchase_order_item):
	rows = frappe.db.sql(
		f"""SELECT {source_expression()} FROM `tabPurchase Receipt Item`
		WHERE purchase_order_item=%s AND docstatus=1 FOR UPDATE""",
		purchase_order_item,
	)
	return sum((number(value) for (value,) in rows), Decimal(0))


def _other_orders(field, detail, parent):
	if field not in ("material_request_item", "sales_order_item"):
		raise ValueError("Unsupported demand identity")
	expression = f"CASE WHEN p.{FLAG}=1 THEN p.{QTY} ELSE p.stock_qty END"
	reference = (
		f"p.`{field}`"
		if field == "material_request_item"
		else "COALESCE(NULLIF(p.sales_order_item,''),m.sales_order_item)"
	)
	# Match the native ordered-quantity counters, which include all submitted orders.
	values = frappe.db.sql(
		f"""SELECT {expression} FROM `tabPurchase Order Item` p
		LEFT JOIN `tabMaterial Request Item` m ON m.name=p.material_request_item
		WHERE {reference}=%s AND p.docstatus=1 AND p.parent<>%s FOR UPDATE""",
		(detail, parent or ""),
	)
	return sum((number(value[0]) for value in values), Decimal(0))


def bind_purchase_order(doc):
	warehouses = configured_warehouses(doc.company)
	if not warehouses:
		return
	requests, orders = {}, {}
	for row in doc.items:
		if row.warehouse not in warehouses:
			continue
		if row.get("material_request") and row.material_request not in requests:
			requests[row.material_request] = frappe.get_doc("Material Request", row.material_request)
		if row.get("sales_order"):
			orders[row.sales_order] = None
	for request in requests.values():
		for line in request.items:
			if line.get("sales_order"):
				orders[line.sales_order] = None
	for name in sorted(orders):
		orders[name] = frappe.get_doc("Sales Order", name, for_update=True)
	for name in sorted(requests):
		requests[name] = frappe.get_doc("Material Request", name, for_update=True)
	planned_mr, planned_so = {}, {}
	for row in doc.items:
		row.set(FLAG, 0)
		row.set(QTY, 0)
		if row.warehouse not in warehouses:
			continue
		request = requests.get(row.get("material_request"))
		source = (
			next((line for line in request.items if line.name == row.get("material_request_item")), None)
			if request
			else None
		)
		if request and row.get("material_request_item") and not source:
			frappe.throw(_("The purchase line does not belong to the linked Material Request."))
		so_name, so_child = row.get("sales_order"), row.get("sales_order_item")
		if source:
			if (
				request.docstatus != 1
				or request.company != doc.company
				or source.item_code != row.item_code
				or source.warehouse != row.warehouse
			):
				frappe.throw("The purchase demand reference is no longer valid.")
			if so_name and (
				so_name != source.get("sales_order") or so_child != source.get("sales_order_item")
			):
				frappe.throw("Purchase Order and Material Request demand references disagree.")
			so_name, so_child = source.get("sales_order"), source.get("sales_order_item")
		if not so_name and not so_child:
			continue
		if doc.get("is_subcontracted") or row.get("delivered_by_supplier"):
			frappe.throw("Quantity-scoped purchases require an ordinary warehouse Purchase Order.")
		order = orders.get(so_name)
		demand = next((line for line in order.items if line.name == so_child), None) if order else None
		if (
			not demand
			or order.docstatus != 1
			or order.company != doc.company
			or order.status in ("Closed", "Completed", "Cancelled", "On Hold")
			or demand.item_code != row.item_code
			or demand.warehouse != row.warehouse
			or demand.stock_uom != row.stock_uom
			or (source and source.stock_uom != row.stock_uom)
		):
			frappe.throw("The purchase line has no current, matching Sales Order demand.")
		row.sales_order = so_name
		row.sales_order_item = so_child
		total = number(row.stock_qty)
		so_budget = max(
			Decimal(0),
			number(demand.stock_qty)
			- _other_orders("sales_order_item", so_child, doc.name)
			- planned_so.get(so_child, Decimal(0)),
		)
		mr_budget = total
		if source:
			mr_budget = max(
				Decimal(0),
				number(source.stock_qty)
				- _other_orders("material_request_item", source.name, doc.name)
				- planned_mr.get(source.name, Decimal(0)),
			)
		allocated = min(total, so_budget, mr_budget)
		if doc._action == "update_after_submit" and row.name:
			received = _received_demand(row.name)
			if allocated < received:
				frappe.throw(_("The order quantity cannot cover the demand already received."))
		row.set(FLAG, 1)
		row.set(QTY, float(allocated))
		planned_so[so_child] = planned_so.get(so_child, Decimal(0)) + allocated
		if source:
			planned_mr[source.name] = planned_mr.get(source.name, Decimal(0)) + allocated


def bind_purchase_receipt(doc):
	if not configured_warehouses(doc.company):
		return
	planned = {}
	for row in doc.items:
		row.set(FLAG, 0)
		row.set(QTY, 0)
		if not row.get("purchase_order_item"):
			continue
		rows = frappe.db.sql(
			f"""SELECT name,parent,item_code,warehouse,stock_uom,{FLAG},{QTY},stock_qty,
			material_request,material_request_item,sales_order,sales_order_item
			FROM `tabPurchase Order Item` WHERE name=%s FOR UPDATE""",
			row.purchase_order_item,
			as_dict=True,
		)
		source = rows[0] if rows else None
		if not source or not source.get(FLAG):
			continue
		if doc.get("is_return"):
			bind_receipt_return(doc, row, source, planned)
			continue
		if (
			source.parent != row.get("purchase_order")
			or source.item_code != row.item_code
			or source.warehouse != row.warehouse
			or source.stock_uom != row.stock_uom
			or any(
				(row.get(key) or None) != (source.get(key) or None)
				for key in ("material_request", "material_request_item", "sales_order", "sales_order_item")
			)
		):
			frappe.throw("The receipt does not retain its exact purchase demand source.")
		receipts = frappe.db.sql(
			f"""SELECT {source_expression()}
			FROM `tabPurchase Receipt Item` WHERE purchase_order_item=%s AND docstatus=1 AND parent<>%s FOR UPDATE""",
			(source.name, doc.name or ""),
		)
		used = sum((number(value[0]) for value in receipts), Decimal(0))
		remaining = max(Decimal(0), effective_quantity(source) - used - planned.get(source.name, Decimal(0)))
		quantity = min(number(row.stock_qty), remaining)
		row.set(FLAG, 1)
		row.set(QTY, float(quantity))
		planned[source.name] = planned.get(source.name, Decimal(0)) + quantity


def bind_receipt_return(doc, row, source, planned):
	"""Reverse only the demand belonging to the exact original receipt child."""
	original_id = row.get("purchase_receipt_item")
	originals = (
		frappe.db.sql(
			f"""SELECT name,parent,item_code,warehouse,stock_uom,stock_qty,
		purchase_order_item,{FLAG},{QTY} FROM `tabPurchase Receipt Item`
		WHERE name=%s AND parent=%s AND docstatus=1 FOR UPDATE""",
			(original_id, doc.get("return_against")),
			as_dict=True,
		)
		if original_id and doc.get("return_against")
		else []
	)
	original = originals[0] if originals else None
	if (
		not original
		or original.purchase_order_item != source.name
		or original.item_code != row.item_code
		or original.warehouse != row.warehouse
		or original.stock_uom != row.stock_uom
		or number(row.stock_qty) >= 0
	):
		frappe.throw("The return must retain its exact original purchase receipt child.")
	returns = frappe.db.sql(
		f"""SELECT stock_qty,{FLAG},{QTY} FROM `tabPurchase Receipt Item`
		WHERE purchase_receipt_item=%s AND docstatus=1 AND parent<>%s FOR UPDATE""",
		(original_id, doc.name or ""),
		as_dict=True,
	)
	returned_stock = -sum((number(r.stock_qty) for r in returns), Decimal(0))
	returned_demand = -sum((effective_quantity(r) for r in returns), Decimal(0))
	used_stock, used_demand = planned.get(original_id, (Decimal(0), Decimal(0)))
	quantity = -number(row.stock_qty)
	if quantity > number(original.stock_qty) - returned_stock - used_stock:
		frappe.throw("The return exceeds the remaining original receipt quantity.")
	demand = min(quantity, max(Decimal(0), effective_quantity(original) - returned_demand - used_demand))
	row.set(FLAG, 1)
	row.set(QTY, -float(demand))
	planned[original_id] = (used_stock + quantity, used_demand + demand)


def lock_purchase_receipt_sources(doc):
	if not configured_warehouses(doc.company):
		return
	for name in sorted({row.purchase_order for row in doc.items if row.purchase_order}):
		frappe.db.get_value("Purchase Order", name, "name", for_update=True)


def guard_stock_invoice(doc, method=None):
	if not doc.get("update_stock") or not configured_warehouses(doc.company):
		return
	details = [row.po_detail for row in doc.items if row.po_detail]
	if details and frappe.db.exists("Purchase Order Item", {"name": ["in", details], FLAG: 1}):
		frappe.throw(_("Receive quantity-scoped purchases through a Purchase Receipt before invoicing."))


def use_demand_quantity(doc, target_doctypes):
	if not configured_warehouses(doc.company):
		return
	for updater in doc.status_updater:
		if updater["target_dt"] in target_doctypes and updater["source_field"] != "fg_item_qty":
			updater["source_field"] = source_expression()
