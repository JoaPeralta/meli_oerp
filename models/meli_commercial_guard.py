"""Protect admitted economic proof and its graph before native side effects.

No context boolean, group membership or sudo grants commercial completeness.
The private importer capability permits draft projection, never confirmation,
invoicing or fulfillment. Existing unaudited native/A-B orders stay native.
"""
from odoo import api, models
from odoo.exceptions import ValidationError

from .meli_economic_scope import economic_projection_allowed
from .meli_order_economics import money


SOURCE = "meli_source_order_item_id"


def _deny():
    raise ValidationError("Economic evidence is conditioned or its protected traceability would change")


def _sources(roots):
    result = roots.env["mercadolibre.orders"]
    if not roots:
        return result
    result |= roots.mapped("meli_orders")
    result |= roots.mapped("meli_order")
    identities = [value for value in roots.mapped("meli_order_id") if value]
    if SOURCE in roots.env["sale.order.line"]._fields:
        result |= roots.order_line.mapped(SOURCE + ".order_id")
    if not result and not identities:
        return result  # Unrelated native/A-B commerce requires no ML model ACL.
    result |= result.search([("sale_order", "in", roots.ids)])
    if identities:
        result |= result.search([("order_id", "in", identities)])
    return result


def _audited(sources):
    return sources.filtered(lambda row: bool(row.economic_snapshot))


def _projection(sources):
    for row in _audited(sources):
        if not economic_projection_allowed(row.env, row.order_id):
            _deny()


def _effects(roots):
    for root in roots:
        if any(source.sale_order != root for source in _audited(_sources(root))):
            _deny()
    for source in _audited(_sources(roots)):
        proof = source.economic_snapshot
        latest = proof.get("latest_observation", proof) if isinstance(proof, dict) else {}
        if (not isinstance(proof, dict) or proof.get("status") != "validated"
                or latest.get("status") == "conditioned_draft"):
            _deny()
        if (proof.get("evidence_version") != 2
                or not any(row.get("status") == "conditioned_draft" for row in proof.get("history", []))):
            # Normal200 shipping/pack/fiscal behavior remains native. Exact
            # scalar revalidation is required only for a conditioned lineage.
            continue
        root = source.sale_order
        items = proof.get("items", [])
        if (not root or proof.get("order_id") != source.order_id
                or proof.get("currency_id") != source.currency_id
                or source.currency_id != root.currency_id.name
                or money(proof.get("commercial_total")) != money(source.total_amount)
                or money(proof.get("buyer_paid")) != money(source.paid_amount)
                or root.currency_id.compare_amounts(root.amount_total, float(money(proof["commercial_total"])))
                or len(root.order_line) != len(items) or SOURCE not in root.order_line._fields):
            _deny()
        for line in root.order_line:
            item = line[SOURCE]
            matched = [row for row in items if row.get("item_id") == item.order_item_id
                       and (row.get("variation_id") or False) == (item.order_item_variation_id or False)]
            if (item.order_id != source or len(matched) != 1 or line.discount
                    or matched[0]["quantity"] != item.quantity or line.product_uom_qty != item.quantity
                    or money(matched[0]["unit_price"]) != money(item.unit_price)
                    or root.currency_id.compare_amounts(line.price_total, float(money(item.unit_price)) * item.quantity)):
                _deny()


def _line_roots(env, values):
    """Read incoming references before super; never mutate relation commands."""
    lines = env["sale.order.line"]
    for command in values or []:
        if isinstance(command, (list, tuple)) and command:
            if command[0] == 6:
                lines |= lines.browse(command[2])
            elif command[0] in (1, 2, 3, 4):
                lines |= lines.browse(command[1])
    return lines.order_id


def _picking_roots(pickings):
    roots = pickings.move_ids.sale_line_id.order_id
    if "sale_id" in pickings._fields:
        roots |= pickings.sale_id
    return roots


def _stock_roots(moves):
    return moves.sale_line_id.order_id | _picking_roots(moves.picking_id)


class EconomicSource(models.Model):
    _inherit = "mercadolibre.orders"

    @api.model_create_multi
    def create(self, vals_list):
        default = self.env.context.get("default_economic_snapshot")
        for vals in vals_list:
            if (vals.get("economic_snapshot", default)
                    and not economic_projection_allowed(self.env, vals.get("order_id"))):
                _deny()
        return super().create(vals_list)

    def write(self, vals):
        protected = {"order_id", "company_id", "seller_id", "seller", "sale_order", "currency_id",
                     "total_amount", "paid_amount", "discount_seller_amount", "coupon_amount",
                     "status", "status_detail", "shipping_id", "shipment", "pack_id"}
        if "economic_snapshot" in vals:
            for row in self:
                if not economic_projection_allowed(row.env, row.order_id):
                    _deny()
        if protected.intersection(vals):
            _projection(self)
        return super().write(vals)

    def unlink(self):
        if _audited(self):
            _deny()
        return super().unlink()

    def copy(self, default=None):
        if _audited(self):
            _deny()
        return super().copy(default)


class EconomicItem(models.Model):
    _inherit = "mercadolibre.order_items"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            _projection(self.env["mercadolibre.orders"].browse(
                vals.get("order_id", self.env.context.get("default_order_id"))))
        return super().create(vals_list)

    def write(self, vals):
        if set(vals).intersection({"order_id", "product_id", "order_item_id", "order_item_variation_id",
                                   "quantity", "unit_price", "full_unit_price", "currency_id", "sale_fee"}):
            _projection(self.order_id | self.env["mercadolibre.orders"].browse(vals.get("order_id")))
        return super().write(vals)

    def unlink(self):
        _projection(self.order_id)
        return super().unlink()

    def copy(self, default=None):
        _projection(self.order_id)
        return super().copy(default)


class EconomicPayment(models.Model):
    _inherit = "mercadolibre.payments"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            _projection(self.env["mercadolibre.orders"].browse(
                vals.get("order_id", self.env.context.get("default_order_id"))))
        return super().create(vals_list)

    def write(self, vals):
        if set(vals).intersection({"order_id", "payment_id", "currency_id", "status", "transaction_amount",
                                   "total_paid_amount", "coupon_amount", "shipping_amount", "full_payment"}):
            _projection(self.order_id | self.env["mercadolibre.orders"].browse(vals.get("order_id")))
        return super().write(vals)

    def unlink(self):
        _projection(self.order_id)
        return super().unlink()

    def copy(self, default=None):
        _projection(self.order_id)
        return super().copy(default)


class EconomicSale(models.Model):
    _inherit = "sale.order"

    def _meli_assert_commercial_effect(self):
        _effects(self)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            incoming = self.env["mercadolibre.orders"]
            remote = vals.get("meli_order_id", self.env.context.get("default_meli_order_id"))
            if remote:
                incoming = incoming.search([("order_id", "=", remote)])
            incoming |= incoming.browse(vals.get("meli_order", self.env.context.get("default_meli_order")))
            for command in vals.get("meli_orders", self.env.context.get("default_meli_orders", [])) or []:
                if command[0] == 6:
                    incoming |= incoming.browse(command[2])
                elif command[0] == 4:
                    incoming |= incoming.browse(command[1])
            _projection(incoming)
            if vals.get("state") in ("sale", "done") and _audited(incoming):
                _deny()
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("state") in ("sale", "done"):
            _effects(self)
        if set(vals).intersection({"meli_order_id", "meli_orders", "meli_order", "company_id", "currency_id",
                                   "partner_id", "partner_invoice_id", "partner_shipping_id", "pricelist_id",
                                   "fiscal_position_id", "order_line", "meli_total_amount", "meli_paid_amount"}):
            _projection(_sources(self))
            incoming = self.env["mercadolibre.orders"]
            incoming |= incoming.browse(vals.get("meli_order"))
            if vals.get("meli_order_id"):
                incoming |= incoming.search([("order_id", "=", vals["meli_order_id"])])
            for command in vals.get("meli_orders", []) or []:
                if command[0] == 6:
                    incoming |= incoming.browse(command[2])
                elif command[0] in (1, 2, 3, 4):
                    incoming |= incoming.browse(command[1])
            _projection(incoming)
        return super().write(vals)

    def unlink(self):
        if _audited(_sources(self)):
            _deny()
        return super().unlink()

    def copy(self, default=None):
        if _audited(_sources(self)):
            _deny()
        return super().copy(default)

    def action_confirm(self):
        _effects(self)
        return super().action_confirm()

    def _action_confirm(self):
        _effects(self)
        return super()._action_confirm()

    def _create_invoices(self, *args, **kwargs):
        _effects(self)
        return super()._create_invoices(*args, **kwargs)


class EconomicSaleLine(models.Model):
    _inherit = "sale.order.line"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            root = self.env["sale.order"].browse(vals.get("order_id", self.env.context.get("default_order_id")))
            _projection(_sources(root))
        return super().create(vals_list)

    def write(self, vals):
        if set(vals).intersection({"order_id", "product_id", SOURCE, "meli_order_item_id",
                                   "meli_order_item_variation_id", "price_unit", "technical_price_unit",
                                   "product_uom_qty", "product_uom_id", "discount", "tax_ids", "display_type"}):
            _projection(_sources(self.order_id | self.env["sale.order"].browse(vals.get("order_id"))))
        return super().write(vals)

    def unlink(self):
        _projection(_sources(self.order_id))
        return super().unlink()

    def copy(self, default=None):
        _projection(_sources(self.order_id))
        return super().copy(default)

    def _action_launch_stock_rule(self, *args, **kwargs):
        _effects(self.order_id)
        return super()._action_launch_stock_rule(*args, **kwargs)


class EconomicInvoice(models.Model):
    _inherit = "account.move"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            for command in list(vals.get("invoice_line_ids", [])) + list(vals.get("line_ids", [])):
                if command[0] in (0, 1) and isinstance(command[2], dict):
                    _effects(_line_roots(self.env, command[2].get("sale_line_ids")))
        return super().create(vals_list)

    def write(self, vals):
        _effects(self.line_ids.sale_line_ids.order_id)
        return super().write(vals)

    def action_post(self):
        _effects(self.line_ids.sale_line_ids.order_id)
        return super().action_post()

    def _post(self, *args, **kwargs):
        _effects(self.line_ids.sale_line_ids.order_id)
        return super()._post(*args, **kwargs)


class EconomicInvoiceLine(models.Model):
    _inherit = "account.move.line"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            _effects(_line_roots(self.env, vals.get("sale_line_ids", self.env.context.get("default_sale_line_ids"))))
        return super().create(vals_list)

    def write(self, vals):
        _effects(self.sale_line_ids.order_id | _line_roots(self.env, vals.get("sale_line_ids")))
        return super().write(vals)

    def unlink(self):
        _effects(self.sale_line_ids.order_id)
        return super().unlink()

    def copy(self, default=None):
        _effects(self.sale_line_ids.order_id)
        return super().copy(default)


class EconomicStockMove(models.Model):
    _inherit = "stock.move"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            _effects(self.env["sale.order.line"].browse(vals.get("sale_line_id",
                self.env.context.get("default_sale_line_id"))).order_id
                | _picking_roots(self.env["stock.picking"].browse(vals.get("picking_id",
                    self.env.context.get("default_picking_id")))))
        return super().create(vals_list)

    def write(self, vals):
        _effects(_stock_roots(self) | self.env["sale.order.line"].browse(vals.get("sale_line_id")).order_id
                 | _picking_roots(self.env["stock.picking"].browse(vals.get("picking_id"))))
        return super().write(vals)

    def _action_confirm(self, *args, **kwargs):
        _effects(_stock_roots(self))
        return super()._action_confirm(*args, **kwargs)

    def _action_assign(self, *args, **kwargs):
        _effects(_stock_roots(self))
        return super()._action_assign(*args, **kwargs)

    def _action_done(self, *args, **kwargs):
        _effects(_stock_roots(self))
        return super()._action_done(*args, **kwargs)

    def unlink(self):
        _effects(_stock_roots(self))
        return super().unlink()

    def copy(self, default=None):
        _effects(_stock_roots(self))
        return super().copy(default)


class EconomicStockMoveLine(models.Model):
    _inherit = "stock.move.line"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            move = self.env["stock.move"].browse(vals.get("move_id", self.env.context.get("default_move_id")))
            _effects(_stock_roots(move))
        return super().create(vals_list)

    def write(self, vals):
        moves = self.move_id | self.env["stock.move"].browse(vals.get("move_id"))
        _effects(_stock_roots(moves))
        return super().write(vals)


class EconomicPicking(models.Model):
    _inherit = "stock.picking"

    @api.model_create_multi
    def create(self, vals_list):
        if "sale_id" in self._fields:
            for vals in vals_list:
                _effects(self.env["sale.order"].browse(vals.get("sale_id", self.env.context.get("default_sale_id"))))
        return super().create(vals_list)

    def write(self, vals):
        _effects(_picking_roots(self))
        if vals.get("sale_id"):
            _effects(self.env["sale.order"].browse(vals["sale_id"]))
        return super().write(vals)

    def action_confirm(self):
        _effects(_picking_roots(self))
        return super().action_confirm()

    def action_assign(self):
        _effects(_picking_roots(self))
        return super().action_assign()

    def button_validate(self):
        _effects(_picking_roots(self))
        return super().button_validate()
