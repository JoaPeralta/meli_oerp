# Upload a verified fiscal PDF to Mercado Libre

The invoice action is manual. It neither creates nor posts an invoice, issues a
CAE, registers payment, activates a cron, nor sends a buyer message.

## Eligibility

- A Mercado Libre manager who can write invoices and has the Odoo invoicing
  group can perform the action in an allowed company.
- The posted customer invoice must cover one sale in full, including its linked
  marketplace orders. The connector checks the existing commercial safeguards,
  verifies each remote order under the company's seller, and checks the complete
  remote pack membership. A fallback to the verified order ID is allowed only
  when the order response explicitly contains a null pack ID.
- The supported ARCA provider is `l10n_ar_arca_edi`. The invoice must have a
  matching **production** authorization attempt, CAE and CAE due date. A
  missing provider, missing evidence, or homologation authorization is refused.
  This addon does not install or configure the ARCA provider.
- The Odoo invoice report must produce a readable, non-encrypted PDF no larger
  than 1,000,000 bytes.

## What the action confirms

The action reads the target's fiscal-document listing before any send. It does not
replace an existing PDF. A PostgreSQL uniqueness constraint and a separately
committed reservation permit at most one outbound attempt per seller and target,
even across workers or companies configured with the same seller. The reservation
survives a request rollback or process crash. The multipart request uses the
existing seller-bound client; it does not retry, follow redirects, or print
credentials, raw response bodies, or invoice content.

Success requires a fresh listing of a valid document tied to the reserved filename
and, when the POST supplied one, the same receipt ID. If a response is lost, use
the **same action** to reconcile by GET. An uncertain or rejected POST leaves the
send fence in place, even if a subsequent listing is empty. This is intentional:
an empty best-effort listing cannot establish that an earlier POST never arrived.
Do not clear the fence or replace a remote PDF as part of normal operation.

## Provenance and deployment boundary

- Workflow reference only: [Aceleradora 19.0 invoice PDF action](https://github.com/aceleradora-la/odoo-marketplaces/blob/19.0/odoo_mercadolibre/models/account_move.py).
- Wire contract: [Mercado Libre fiscal documents](https://developers.mercadolibre.com.ar/cargar-factura).
- Rendering: [Odoo 19 Argentina localization](https://www.odoo.com/documentation/19.0/applications/finance/fiscal_localizations/argentina.html).
- The local implementation adds a journal table and a native invoice view; an
  existing database needs a separately approved module upgrade. Rollback of the
  code must not discard the journal or any pending reservations.
