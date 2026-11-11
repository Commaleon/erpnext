# Copyright (c) 2026, Commaleon and Contributors
# License: GNU General Public License v3. See license.txt

"""Create linked Purchase Material Requests for uncovered Sales Order quantities."""

from collections import defaultdict

import frappe
from frappe import _
from frappe.utils import flt, nowdate


def configured_warehouses(company):
	config = frappe.conf.get("erpnext_auto_procurement") or {}
	if not config.get("enabled") or config.get("company") != company:
		return ()
	if (frappe.conf.get("commaleon_procurement") or {}).get("enabled"):
		frappe.throw(_("Disable Commaleon Ops procurement before enabling ERPNext automatic procurement."))
	return tuple(config.get("warehouses") or ())


def enqueue_shortage_request(doc, method=None):
	if configured_warehouses(doc.company):
		frappe.enqueue(create_shortage_request, enqueue_after_commit=True, sales_order=doc.name)


def create_shortage_request(sales_order):
	from erpnext.selling.doctype.sales_order.mapper import make_material_request

	order = frappe.get_doc("Sales Order", sales_order, for_update=True)
	warehouses = configured_warehouses(order.company)
	if not warehouses or order.docstatus != 1 or order.status in ("Closed", "Completed", "On Hold"):
		return

	needed = _shortage_quantities(order, warehouses)
	if not needed:
		return

	# A sales user may submit the source without having Buying permissions.
	user = frappe.session.user
	frappe.set_user("Administrator")
	try:
		request = make_material_request(order.name)
		request.transaction_date = nowdate()
		request.material_request_type = "Purchase"
		request.set("items", [row for row in request.items if row.sales_order_item in needed])
		if {row.sales_order_item for row in request.items} != needed.keys():
			frappe.throw(_("Material Request mapping did not preserve the Sales Order item links."))
		for row in request.items:
			row.uom = row.stock_uom
			row.conversion_factor = 1
			row.qty = row.stock_qty = needed[row.sales_order_item]
			row.amount = row.qty * flt(row.rate)
		request.insert()
		request.submit()
		return request.name
	finally:
		frappe.set_user(user)


def _shortage_quantities(order, warehouses):
	from erpnext.stock.doctype.packed_item.packed_item import is_product_bundle

	eligible_lines = {
		row.name: row
		for row in order.items
		if row.warehouse in warehouses
		and not row.delivered_by_supplier
		and not row.closed
		and not is_product_bundle(row.item_code)
	}
	if not eligible_lines:
		return {}

	params = {
		"company": order.company,
		"items": tuple(sorted({row.item_code for row in eligible_lines.values()})),
		"warehouses": warehouses,
	}
	stock = frappe.db.sql(
		"""SELECT item_code, warehouse,
			GREATEST(0, actual_qty - reserved_stock - reserved_qty_for_production
				- reserved_qty_for_sub_contract - reserved_qty_for_production_plan) AS available
		FROM `tabBin` WHERE item_code IN %(items)s AND warehouse IN %(warehouses)s""",
		params,
	)
	available = defaultdict(float, {(item, warehouse): flt(qty) for item, warehouse, qty in stock})
	needed = {}
	for row in _open_sales_lines(params):
		key = (row.item_code, row.warehouse)
		uncovered = max(0, flt(row.open_qty) - flt(row.covered_qty) - flt(row.reserved_qty))
		from_stock = min(uncovered, available[key])
		available[key] -= from_stock
		if line := eligible_lines.get(row.name):
			# The mapper excludes quantities already covered by a submitted request.
			shortage = min(uncovered - from_stock, flt(row.open_qty) - flt(line.requested_qty))
			if shortage > 0:
				needed[row.name] = shortage
	return needed


def _open_sales_lines(params):
	remaining_purchase_demand = "poi.stock_qty"
	if frappe.db.has_column("Purchase Order Item", "purchase_demand_managed") and frappe.db.has_column(
		"Purchase Receipt Item", "purchase_demand_managed"
	):
		remaining_purchase_demand = """CASE WHEN poi.purchase_demand_managed = 1 THEN poi.purchase_demand_qty - COALESCE((
						SELECT SUM(CASE WHEN pri.purchase_demand_managed = 1 THEN pri.purchase_demand_qty
							ELSE pri.stock_qty END)
						FROM `tabPurchase Receipt Item` pri
						WHERE pri.docstatus = 1 AND pri.purchase_order_item = poi.name
					), 0) ELSE poi.stock_qty END"""

	return frappe.db.sql(
		f"""SELECT soi.name, soi.item_code, soi.warehouse,
			soi.stock_qty - GREATEST(0, soi.delivered_qty) * soi.conversion_factor AS open_qty,
			soi.stock_reserved_qty AS reserved_qty,
			COALESCE((
				SELECT SUM(GREATEST(0, mri.stock_qty - GREATEST(mri.ordered_qty, mri.received_qty)))
				FROM `tabMaterial Request Item` mri JOIN `tabMaterial Request` mr ON mr.name = mri.parent
				WHERE mri.sales_order_item = soi.name AND mri.warehouse = soi.warehouse
					AND mr.docstatus = 1 AND mr.status != 'Stopped' AND mr.material_request_type = 'Purchase'
			), 0) + COALESCE((
				SELECT SUM(GREATEST(0, LEAST(poi.stock_qty - poi.received_qty * poi.conversion_factor,
					{remaining_purchase_demand})))
				FROM `tabPurchase Order Item` poi JOIN `tabPurchase Order` po ON po.name = poi.parent
				LEFT JOIN `tabMaterial Request Item` mri ON mri.name = poi.material_request_item
				WHERE COALESCE(NULLIF(poi.sales_order_item, ''), mri.sales_order_item) = soi.name
					AND poi.warehouse = soi.warehouse AND po.docstatus = 1 AND po.status NOT IN ('Closed', 'Stopped')
			), 0) AS covered_qty
		FROM `tabSales Order Item` soi JOIN `tabSales Order` so ON so.name = soi.parent
		JOIN `tabItem` item ON item.name = soi.item_code
		WHERE so.docstatus = 1 AND so.company = %(company)s AND so.status NOT IN ('Closed', 'Completed', 'On Hold')
			AND soi.item_code IN %(items)s AND soi.warehouse IN %(warehouses)s
			AND item.is_stock_item = 1 AND soi.delivered_by_supplier = 0 AND soi.closed = 0
			AND soi.stock_qty > GREATEST(0, soi.delivered_qty) * soi.conversion_factor
		ORDER BY so.creation, so.name, soi.idx, soi.name""",
		params,
		as_dict=True,
	)
