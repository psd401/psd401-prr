"""Minimal-PII serializers for public request intake."""

from rest_framework import serializers


class PublicIntakeSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    email = serializers.EmailField()
    phone = serializers.CharField(max_length=40, required=False, allow_blank=True)
    street_address = serializers.CharField(
        max_length=1000,
        required=False,
        allow_blank=True,
    )
    city = serializers.CharField(max_length=255, required=False, allow_blank=True)
    state = serializers.CharField(max_length=100, required=False, allow_blank=True)
    postal_code = serializers.CharField(
        max_length=32,
        required=False,
        allow_blank=True,
    )
    company = serializers.CharField(max_length=255, required=False, allow_blank=True)
    description = serializers.CharField(max_length=100_000)
    website = serializers.CharField(
        required=False,
        allow_blank=True,
        write_only=True,
        trim_whitespace=False,
    )
    attachments = serializers.ListField(
        child=serializers.FileField(),
        required=False,
        write_only=True,
    )


class PublicIntakeResponseSerializer(serializers.Serializer):
    number = serializers.CharField()
