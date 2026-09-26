# src/accounts/migrations/0001_initial.py
# Written by makemigrations under Django 5.2.17, then wrapped to 99 columns. The wrapped file
# parses to the same syntax tree as the generated one, whose 2188 bytes are
# sha256 9a4365892828f72053de54deb4e8c31f170e26f5e62b5c589a5c00a38c052c52.
# Do not hand-edit this file. Change src/accounts/models.py and regenerate: the image build
# runs makemigrations --check --dry-run, which fails if the model and this file disagree.

import accounts.models
import django.db.models.functions.text
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.CreateModel(
            name='AppUser',
            fields=[
                ('id', models.BigAutoField(
                    auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('password', models.CharField(max_length=128, verbose_name='password')),
                ('last_login', models.DateTimeField(
                    blank=True, null=True, verbose_name='last login')),
                ('is_superuser', models.BooleanField(
                    default=False,
                    help_text='Designates that this user has all permissions without '
                              'explicitly assigning them.',
                    verbose_name='superuser status')),
                ('email', models.EmailField(
                    max_length=254, unique=True, verbose_name='email address')),
                ('display_name', models.TextField(blank=True, default='')),
                ('is_active', models.BooleanField(default=True)),
                ('is_staff', models.BooleanField(default=False)),
                ('date_joined', models.DateTimeField(default=django.utils.timezone.now)),
                ('groups', models.ManyToManyField(
                    blank=True,
                    help_text='The groups this user belongs to. A user will get all '
                              'permissions granted to each of their groups.',
                    related_name='user_set', related_query_name='user', to='auth.group',
                    verbose_name='groups')),
                ('user_permissions', models.ManyToManyField(
                    blank=True, help_text='Specific permissions for this user.',
                    related_name='user_set', related_query_name='user',
                    to='auth.permission', verbose_name='user permissions')),
            ],
            options={
                'db_table': 'app_user',
                'constraints': [models.UniqueConstraint(
                    django.db.models.functions.text.Lower('email'),
                    name='app_user_email_ci_unique')],
            },
            managers=[
                ('objects', accounts.models.AppUserManager()),
            ],
        ),
    ]
