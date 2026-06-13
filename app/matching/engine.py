"""Motor de matching — F4.

TODO(F4): FTS sobre items/productos.

El tsv de licitaciones y compras_agiles (columna GENERATED STORED) solo indexa
nombre + descripcion del padre; no puede incluir nombres de licitacion_items ni
ca_productos porque las columnas generadas no pueden leer tablas hijas.

La query de busqueda FTS debe combinar ambas fuentes:

    -- licitaciones
    SELECT l.* FROM licitaciones l
    WHERE l.tsv @@ query
       OR EXISTS (
           SELECT 1 FROM licitacion_items i
           WHERE i.licitacion_codigo = l.codigo
             AND to_tsvector('spanish', inmutable_unaccent(i.nombre)) @@ query
       )

    -- compras_agiles
    SELECT ca.* FROM compras_agiles ca
    WHERE ca.tsv @@ query
       OR EXISTS (
           SELECT 1 FROM ca_productos p
           WHERE p.ca_codigo = ca.codigo
             AND to_tsvector('spanish', inmutable_unaccent(p.nombre)) @@ query
       )

Implementar en F4 al construir score_licitacion() y score_compra_agil().
"""
