# -*- coding: utf-8 -*-
##############################################################################
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
##############################################################################
#
# Mutable MercadoLibre authentication state, one row per company.
#
# WHY IT IS NOT ON res.company
# ----------------------------
# Odoo 19 puts every cursor at REPEATABLE READ (odoo/sql_db.py, Cursor.__init__,
# unconditional). Renewing a token in its own transaction — which is required,
# because MercadoLibre's refresh tokens are single-use and a business rollback
# must not undo a rotation MercadoLibre already performed — then has two
# consequences against the ambient business transaction:
#
#   1. the ambient snapshot can never see the new credentials, and
#   2. any later UPDATE of that same res_company row by the ambient
#      transaction aborts with "could not serialize access due to concurrent
#      update", taking a multi-minute import down with it.
#
# (2) is the damaging one, and it is not hypothetical: several MercadoLibre
# paths write res.company, and get_new_instance runs dozens of times per import.
#
# Keeping AUTH safe by auditing every present and future write() on res.company
# is not a property, it is a chore that fails silently the first time someone
# forgets. Separating the domains removes the contention by construction:
#
#     BUSINESS  ->  res.company / products / postings
#     AUTH      ->  mercadolibre.auth
#
# WHAT LIVES HERE
# ---------------
# Only the state AUTH must MUTATE. client_id, client secret, seller id,
# redirect uri and the cron settings stay on res.company: AUTH only READS them,
# and reading res_company from the AUTH transaction creates no conflict.
#
# `code` is here for the same reason as the tokens and not for tidiness: the
# refresh path writes it in the same write() as the credentials, so leaving it
# on res.company would force AUTH to touch res_company and reintroduce exactly
# the conflict this model removes.
#
# Storing these values in plain columns remains an open item, tracked
# separately: this module has never had field-level encryption for them, and
# moving them does not change that either way.

from odoo import fields, models

from . import versions


class MercadolibreAuth(models.Model):
    _name = 'mercadolibre.auth'
    _description = 'MercadoLibre authentication state'
    _rec_name = 'company_id'

    company_id = fields.Many2one(
        'res.company', string='Company', required=True,
        ondelete='cascade', index=True)

    access_token = fields.Char(string='Access Token', size=256,
                               groups="base.group_system")
    refresh_token = fields.Char(string='Refresh Token', size=256,
                                groups="base.group_system")
    code = fields.Char(string='Code', size=256,
                       groups="base.group_system")

    token_expires_in = fields.Integer(
        string='Token Lifetime (s)',
        help='Lifetime MercadoLibre reported for the current token. '
             '0 means it was not reported.')
    token_refreshed_at = fields.Datetime(
        string='Token Received At',
        help='When the current token was received.')
    token_expires_at = fields.Datetime(
        string='Token Expires At',
        help='Derived from the received time plus the reported lifetime. '
             'Empty when MercadoLibre did not report a lifetime.')

    _unique_company_id = versions.UniqueIndex(
        'company_id', message='One MercadoLibre auth row per company.')
    _sql_constraints = versions.sql_constraints_if_no_unique_index([
        ('unique_company_id', 'company_id',
         'One MercadoLibre auth row per company.'),
    ])
