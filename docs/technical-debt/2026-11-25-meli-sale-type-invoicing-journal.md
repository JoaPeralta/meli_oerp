# Deuda técnica: tipo de venta y diario de facturación de Mercado Libre

**Fecha:** 2026-11-25. **Estado:** pendiente de verificación; no bloquea la primera factura manual.

## Contexto
Odoo 19 Community; meli_oerp 19.0.26.202 (pin `31c2b4f308ea8325ad0b27ccdef0a2ba44cc8b87`); OCA sale_order_type (sale-workflow pin `9c8c601845ff30bd1fbe2e871fa3c5249b3bffe7`).

Desde la interfaz se creó el diario **Ventas Mercado Libre**, con documentos fiscales, PV ARCA 6, prefijo 00006 y sistema Web Service. Mercado Libre se configuró como «Otro facturador». El PV 6 se destina exclusivamente al canal; no compartirlo con otro emisor activo. En producción solo se visualizó el tipo **Pedido Normal**, sin diario asignado. No se creó ni modificó ningún tipo de venta.

## Hallazgos
- Odoo 19 ofrece `sale.order.journal_id` (Diario de facturación), visible en modo desarrollador en la vista estándar. Su `_prepare_invoice` transmite el diario a la factura cuando está definido.
- OCA ofrece `sale.order.type.journal_id`. Su `_prepare_invoice` aplica el diario del tipo después de super, con prioridad sobre el diario nativo si está configurado.
- El conector desplegado tiene una asignación condicional de `mercadolibre_invoice_journal_id` al pedido si ambos campos existen (models/orders.py). La verificación en producción informó que el campo de configuración y su vista no existen.
- Falta comprobar el comportamiento efectivo de la importación y la interacción con Circuito A/B.

## Propuesta a verificar
Preferir las funciones existentes de Odoo/OCA a un selector redundante del conector. Evaluar un tipo de pedido Mercado Libre asociado al diario Ventas Mercado Libre y su asignación al importar, únicamente si resulta compatible con Circuito A/B. Si requiere código, limitarlo a la integración mínima.

## Criterios de aceptación
- Comprobar precedencia efectiva de diario nativo, tipo de venta y reglas de Circuito A/B.
- Pedidos individuales y packs generan facturas en el diario correcto; otros canales quedan intactos.
- No alterar por defecto almacenes, rutas, listas de precios, condiciones de pago, secuencias de pedidos ni circuitos ajenos.
- Probar reimportación e idempotencia; no reasignar facturas publicadas.
- No activar emisión automática ni crons sin autorización. Probar primero fuera de producción.

## Prioridad inmediata
Completar la primera factura real mediante selección manual del diario y verificarlo antes de publicar. Comprobar ausencia de duplicación fiscal, elegibilidad de la venta y continuidad de numeración por tipo en ARCA. No crear comprobantes ficticios ni editar la base para inicializar la secuencia.

## Referencias
- [Conector desplegado](https://github.com/JoaPeralta/meli_oerp/blob/31c2b4f308ea8325ad0b27ccdef0a2ba44cc8b87/models/orders.py#L5390-L5396)
- [Odoo 19](https://github.com/odoo/odoo/blob/19.0/addons/sale/models/sale_order.py#L141-L146)
- [OCA sale_order_type](https://github.com/OCA/sale-workflow/blob/9c8c601845ff30bd1fbe2e871fa3c5249b3bffe7/sale_order_type/models/sale.py#L200-L206)
