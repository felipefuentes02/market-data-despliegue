from django.db import models

class AjusteInventario(models.Model):
    id_ajuste = models.AutoField(primary_key=True)
    cod_barra = models.ForeignKey('Producto', models.DO_NOTHING, db_column='cod_barra')
    rut_tienda = models.ForeignKey('Tienda', models.DO_NOTHING, db_column='rut_tienda')
    fecha_ajuste = models.DateTimeField()
    cantidad = models.IntegerField()
    motivo = models.CharField(max_length=50)
    id_usuario = models.ForeignKey('Usuario', models.DO_NOTHING, db_column='id_usuario')

    class Meta:
        managed = False
        db_table = 'ajuste_inventario'

    def save(self, *args, **kwargs):
        if self.motivo:
            self.motivo = self.motivo.upper()

        super(AjusteInventario, self).save(*args, **kwargs)

class ClienteFiado(models.Model):
    rut = models.CharField(primary_key=True, max_length=15) 
    nombre = models.CharField(max_length=40)
    apellido = models.CharField(max_length=40)

    class Meta:
        managed = False
        db_table = 'cliente_fiado'

    def save(self, *args, **kwargs):
        if self.nombre:
            self.nombre = self.nombre.upper()
        if self.apellido:
            self.apellido = self.apellido.upper()
        
        super(ClienteFiado, self).save(*args, **kwargs)

class Comuna(models.Model):
    id_comuna = models.AutoField(primary_key=True)
    nombre_comuna = models.CharField(max_length=60)
    region = models.CharField(max_length=60)

    class Meta:
        managed = False
        db_table = 'comuna'

    def save(self, *args, **kwargs):
        if self.nombre_comuna:
            self.nombre_comuna = self.nombre_comuna.upper()
        if self.region:
            self.region = self.region.upper()
        
        super(Comuna, self).save(*args, **kwargs)

class DetalleFactura(models.Model):
    id_detalle_factura = models.AutoField(primary_key=True)
    cod_barra = models.ForeignKey('Producto', models.DO_NOTHING, db_column='cod_barra')
    cantidad = models.FloatField()
    valor_compra = models.IntegerField()
    
    folio_factura = models.ForeignKey('Factura', models.DO_NOTHING, db_column='folio_factura')

    class Meta:
        managed = False
        db_table = 'detalle_factura'


class DetalleVenta(models.Model):
    id_detalle = models.AutoField(primary_key=True)
    id_venta = models.ForeignKey('Venta', models.DO_NOTHING, db_column='id_venta')
    cod_barra = models.ForeignKey('Producto', models.DO_NOTHING, db_column='cod_barra')
    cantidad = models.FloatField()
    precio_unitario = models.IntegerField() 

    class Meta:
        managed = False
        db_table = 'detalle_venta'

class DuenoTienda(models.Model):
    id_dueno = models.AutoField(primary_key=True)
    nombre = models.CharField(max_length=25)
    primer_apellido = models.CharField(max_length=25)
    segundo_apellido = models.CharField(max_length=25)

    class Meta:
        managed = False
        db_table = 'dueno_tienda'

    def save(self, *args, **kwargs):
        if self.nombre:
            self.nombre = self.nombre.upper()
        if self.primer_apellido:
            self.primer_apellido = self.primer_apellido.upper()
        if self.segundo_apellido:
            self.segundo_apellido = self.segundo_apellido.upper()
        
        super(DuenoTienda, self).save(*args, **kwargs)

class Factura(models.Model):
    id_factura = models.AutoField(primary_key=True)
    folio_factura = models.IntegerField()
    es_compra_directa = models.BooleanField()
    fecha_emision = models.DateField()
    fecha_ingreso = models.DateField()
    rut_tienda = models.ForeignKey('Tienda', models.DO_NOTHING, db_column='rut_tienda', blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'factura'

class Inventario(models.Model):
    id = models.AutoField(primary_key=True)    
    cod_barra = models.ForeignKey('Producto', models.DO_NOTHING, db_column='cod_barra')
    rut_tienda = models.ForeignKey('Tienda', models.DO_NOTHING, db_column='rut_tienda')
    stock_actual = models.FloatField()
    precio_venta = models.IntegerField()
    umbral_seguridad = models.IntegerField(null=True, blank=True)

    class Meta:
        managed = False
        db_table = 'inventario'
        unique_together = (('cod_barra', 'rut_tienda'),)

class Producto(models.Model):
    cod_barra = models.DecimalField(primary_key=True, max_digits=20, decimal_places=0)
    descripcion = models.CharField(max_length=120)
    volumen = models.IntegerField()
    marca = models.CharField(max_length=80)
    fabricante = models.CharField(max_length=80)
    categoria = models.CharField(max_length=50)
    precio_venta = models.IntegerField()

    class Meta:
        managed = False
        db_table = 'producto'

    def save(self, *args, **kwargs):
        if self.descripcion:
            self.descripcion = self.descripcion.upper()
        if self.marca:
            self.marca = self.marca.upper()
        if self.fabricante:
            self.fabricante = self.fabricante.upper()
        if self.categoria:
            self.categoria = self.categoria.upper()
        
        super(Producto, self).save(*args, **kwargs)

class Tienda(models.Model):
    rut_tienda = models.CharField(primary_key=True, max_length=20)
    nombre = models.CharField(max_length=60)
    tipo_tienda = models.CharField(max_length=30)
    calle = models.CharField(max_length=60)
    numero = models.IntegerField()
    detalle = models.CharField(max_length=60, blank=True, null=True)
    id_comuna = models.IntegerField()
    id_dueno = models.IntegerField()

    class Meta:
        managed = False
        db_table = 'tienda'

    def save(self, *args, **kwargs):
        if self.nombre:
            self.nombre = self.nombre.upper()
        if self.tipo_tienda:
            self.tipo_tienda = self.tipo_tienda.upper()
        if self.calle:
            self.calle = self.calle.upper()
        if self.detalle:
            self.detalle = self.detalle.upper()
        
        super(Tienda, self).save(*args, **kwargs)

class Usuario(models.Model):
    id_usuario = models.AutoField(primary_key=True)
    nombre_usuario = models.CharField(unique=True, max_length=40)
    nombre = models.CharField(max_length=25)
    primer_apellido = models.CharField(max_length=25)
    segundo_apellido = models.CharField(max_length=25)
    rol = models.CharField(max_length=50)
    mail = models.CharField(max_length=100)
    ultimo_ingreso = models.DateTimeField(blank=True, null=True)
    password = models.CharField(max_length=255)
    requiere_cambio_pass = models.BooleanField(default=False)
    es_activo = models.BooleanField()
    fecha_creacion = models.DateTimeField()
    rut_tienda = models.ForeignKey('Tienda', models.DO_NOTHING, db_column='rut_tienda', blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'usuario'

    def save(self, *args, **kwargs):
        if self.nombre:
            self.nombre = self.nombre.upper()
        if self.primer_apellido:
            self.primer_apellido = self.primer_apellido.upper()
        if self.segundo_apellido:
            self.segundo_apellido = self.segundo_apellido.upper()
        if self.rol:
            self.rol = self.rol.upper()
        if self.mail:
            self.mail = self.mail.upper()
        
        super(Usuario, self).save(*args, **kwargs)

class Venta(models.Model):
    id_venta = models.AutoField(primary_key=True)
    fecha_venta = models.DateTimeField()
    total_neto = models.IntegerField()
    iva = models.IntegerField()
    total_bruto = models.IntegerField()
    estado_pago = models.BooleanField()
    rut_tienda = models.ForeignKey('Tienda', models.DO_NOTHING, db_column='rut_tienda')
    id_usuario = models.ForeignKey('Usuario', models.DO_NOTHING, db_column='id_usuario')
    rut_cliente = models.ForeignKey('ClienteFiado', models.DO_NOTHING, db_column='rut_cliente', blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'venta'

class AbonoFiado(models.Model):
    id_abono = models.AutoField(primary_key=True)
    fecha_pago = models.DateTimeField()
    monto = models.IntegerField()
    rut_cliente = models.ForeignKey('ClienteFiado', models.DO_NOTHING, db_column='rut_cliente')
    id_usuario = models.ForeignKey('Usuario', models.DO_NOTHING, db_column='id_usuario')
    rut_tienda = models.ForeignKey('Tienda', models.DO_NOTHING, db_column='rut_tienda', blank=True, null=True)

    class Meta:
        managed = False
        db_table = 'abono_fiado'

class CajaSesion(models.Model):
    id_sesion = models.AutoField(primary_key=True)
    id_usuario = models.ForeignKey('Usuario', models.DO_NOTHING, db_column='id_usuario')
    rut_tienda = models.ForeignKey('Tienda', models.DO_NOTHING, db_column='rut_tienda')
    fecha_apertura = models.DateTimeField()
    fecha_cierre = models.DateTimeField(null=True, blank=True)
    monto_apertura = models.IntegerField()
    monto_cierre_real = models.IntegerField(null=True, blank=True)
    monto_cierre_esperado = models.IntegerField(null=True, blank=True)
    estado = models.BooleanField(default=True)

    class Meta:
        managed = False
        db_table = 'caja_sesion'